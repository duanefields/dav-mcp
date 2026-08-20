"""The CalDAV client: discovery, calendar listing, queries, and writes.

Everything Apple-specific is confined to this module. The shape of the protocol
work follows what iCloud actually answered when probed:

* ``OPTIONS`` on the principal advertises ``calendar-auto-schedule``, so the
  server sends iMIP invitations itself when an event carries an ORGANIZER and
  ATTENDEEs. We never speak SMTP.
* ``calendar-query`` honors ``<C:expand>``, so recurring events come back as one
  response per occurrence with a ``RECURRENCE-ID``. No local expansion is
  needed, and no local cache either: measured against a calendar of several
  thousand events, a two-week window answered in well under a second and a full
  year in about twice that.
* Text search cannot be combined with a time range: iCloud silently ignores
  a ``prop-filter`` whenever a ``time-range`` is present, and rejects the
  reversed order with a 412. Matching therefore happens above this layer.

Discovery (principal -> calendar-home-set -> calendars) is cached for
``_DISCOVERY_TTL`` because it costs two extra round trips and essentially never
changes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

DEFAULT_ROOT = "https://caldav.icloud.com"
USER_AGENT = "calendar-mcp/0.1"
_DISCOVERY_TTL = 300.0

# iCloud starts answering 503 after a burst of writes. It clears on its own, so
# a short retry turns a transient blip into a slow success.
#
# Deliberately brief. iCloud sends `Retry-After: 30` when it is throttling in
# earnest, and honoring that literally makes a tool call hang for a minute and a
# half -- far worse than failing in a couple of seconds with a message saying to
# try again. Retry the blips; report the real throttling.
_RETRY_STATUSES = (429, 503)
_RETRY_BACKOFF = (1.0, 3.0)
_RETRY_AFTER_CAP = 5.0

DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
ICAL_NS = "{http://apple.com/ns/ical/}"

_PROPFIND_PRINCIPAL = (
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/>'
    "</d:prop></d:propfind>"
)

_PROPFIND_HOME = (
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><c:calendar-home-set/><c:calendar-user-address-set/>"
    "</d:prop></d:propfind>"
)

_PROPFIND_CALENDARS = (
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
    'xmlns:i="http://apple.com/ns/ical/">'
    "<d:prop><d:displayname/><d:resourcetype/><d:current-user-privilege-set/>"
    "<c:supported-calendar-component-set/><i:calendar-color/>"
    "</d:prop></d:propfind>"
)


class CalDavError(Exception):
    """A CalDAV request failed in a way the caller should surface."""


class NotFound(CalDavError):
    """The requested resource does not exist on the server."""


class Conflict(CalDavError):
    """The resource changed underneath us, or already existed when creating."""


class AuthError(CalDavError):
    """The Apple ID or app-specific password was rejected."""


class Throttled(CalDavError):
    """iCloud is refusing requests for now. Not a credential problem."""


@dataclass(frozen=True)
class Calendar:
    """One writable collection on the server."""

    id: str
    name: str
    color: str
    url: str
    components: tuple[str, ...]
    read_only: bool

    @property
    def supports_events(self) -> bool:
        return "VEVENT" in self.components


@dataclass(frozen=True)
class Resource:
    """One ``.ics`` resource, or one expanded occurrence of one."""

    calendar_id: str
    name: str
    url: str
    etag: str
    ics: str


class CalDavClient:
    """A thin async CalDAV client scoped to a single account."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        root: str = DEFAULT_ROOT,
        timeout: float = 30.0,
    ):
        if not username or not password:
            raise ValueError("username and password are both required")
        self._root = root.rstrip("/")
        self._auth = httpx.BasicAuth(username, password)
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

        self._home: str | None = None
        self._addresses: tuple[str, ...] = ()
        self._calendars: list[Calendar] = []
        self._discovered_at = 0.0

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                auth=self._auth,
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        body: str | None = None,
        headers: dict[str, str] | None = None,
        expect: tuple[int, ...] = (200, 207),
    ) -> httpx.Response:
        response = None
        for attempt, pause in enumerate((*_RETRY_BACKOFF, None)):
            try:
                response = await self._http().request(
                    method,
                    url,
                    content=body.encode("utf-8") if body else None,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise CalDavError(f"{method} {url} failed: {exc}") from exc

            if response.status_code not in _RETRY_STATUSES or pause is None:
                break

            # Honor Retry-After when iCloud bothers to send one.
            delay = pause
            retry_after = response.headers.get("Retry-After", "").strip()
            if retry_after.isdigit():
                delay = max(delay, min(float(retry_after), _RETRY_AFTER_CAP))
            logger.warning(
                "%s %s returned %s; retrying in %.0fs (attempt %d)",
                method,
                url,
                response.status_code,
                delay,
                attempt + 1,
            )
            await asyncio.sleep(delay)

        assert response is not None

        if response.status_code in _RETRY_STATUSES:
            raise Throttled(
                f"iCloud is temporarily refusing requests ({response.status_code}) "
                "and did not recover after several retries. This is rate limiting, "
                "not a credential problem -- it usually clears within a few "
                "minutes. Wait and try again."
            )

        if response.status_code in (401, 403):
            raise AuthError(
                "iCloud rejected the credentials. Check APPLE_ID and "
                "APPLE_APP_PASSWORD -- the password must be an app-specific "
                "password from appleid.apple.com, not the account password."
            )
        if response.status_code == 404:
            raise NotFound(f"{url} does not exist")
        if response.status_code in (409, 412):
            raise Conflict(
                f"{method} {url} was refused as a conflict ({response.status_code}); "
                "the event changed on the server since it was read."
            )
        if response.status_code not in expect:
            raise CalDavError(
                f"{method} {url} returned {response.status_code}: "
                f"{response.text[:400]}"
            )
        return response

    async def _propfind(self, url: str, body: str, depth: str = "0") -> ET.Element:
        response = await self._request(
            "PROPFIND",
            url,
            body=body,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            expect=(207,),
        )
        return ET.fromstring(response.text)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def _discover(self, *, force: bool = False) -> None:
        if not force and self._home and time.time() - self._discovered_at < _DISCOVERY_TTL:
            return

        tree = await self._propfind(self._root + "/", _PROPFIND_PRINCIPAL)
        href = tree.find(f".//{DAV}current-user-principal/{DAV}href")
        if href is None or not href.text:
            raise CalDavError(
                "The server did not return a current-user-principal. This usually "
                "means the Apple ID or app-specific password is wrong."
            )
        principal = urljoin(self._root + "/", href.text)

        tree = await self._propfind(principal, _PROPFIND_HOME)
        home_href = tree.find(f".//{CAL}calendar-home-set/{DAV}href")
        if home_href is None or not home_href.text:
            raise CalDavError("The server did not return a calendar-home-set.")
        self._home = urljoin(principal, home_href.text)

        self._addresses = tuple(
            el.text.lower()
            for el in tree.findall(f".//{CAL}calendar-user-address-set/{DAV}href")
            if el.text and el.text.lower().startswith("mailto:")
        )

        self._calendars = await self._read_calendars(self._home)
        self._discovered_at = time.time()
        logger.info(
            "Discovered %d calendar(s) under %s", len(self._calendars), self._home
        )

    async def _read_calendars(self, home: str) -> list[Calendar]:
        tree = await self._propfind(home, _PROPFIND_CALENDARS, depth="1")
        found: list[Calendar] = []

        for response in tree.findall(f"{DAV}response"):
            href_el = response.find(f"{DAV}href")
            if href_el is None or not href_el.text:
                continue
            url = urljoin(home, href_el.text)
            if url.rstrip("/") == home.rstrip("/"):
                continue

            resourcetype = response.find(f".//{DAV}resourcetype")
            kinds = (
                {child.tag for child in resourcetype}
                if resourcetype is not None
                else set()
            )
            if f"{CAL}calendar" not in kinds:
                continue
            # A subscribed collection mirrors someone else's feed and cannot be
            # written to. iCloud leaves `calendar` off its resourcetype so the
            # check above already drops it, but other servers do not, and the
            # namespace differs between them -- hence matching on local name.
            if any(tag.rsplit("}", 1)[-1] == "subscribed" for tag in kinds):
                continue

            components = tuple(
                comp.get("name", "")
                for comp in response.findall(
                    f".//{CAL}supported-calendar-component-set/{CAL}comp"
                )
            )
            name_el = response.find(f".//{DAV}displayname")
            color_el = response.find(f".//{ICAL_NS}calendar-color")
            privileges = {
                priv.tag
                for priv in response.findall(
                    f".//{DAV}current-user-privilege-set//{DAV}privilege/*"
                )
            }

            found.append(
                Calendar(
                    id=_calendar_id(url),
                    name=(name_el.text or "").strip() if name_el is not None else "",
                    color=_normalize_color(color_el.text if color_el is not None else ""),
                    url=url,
                    components=components,
                    read_only=f"{DAV}write-content" not in privileges,
                )
            )

        return found

    async def calendars(self, *, events_only: bool = True) -> list[Calendar]:
        """All calendars on the account, event calendars only by default."""
        await self._discover()
        if events_only:
            return [cal for cal in self._calendars if cal.supports_events]
        return list(self._calendars)

    async def calendar(self, calendar_id: str) -> Calendar:
        """Look up one calendar by id, refreshing discovery if it is unknown."""
        for cal in await self.calendars(events_only=False):
            if cal.id == calendar_id:
                return cal
        # A calendar created since the last discovery is the common case here.
        await self._discover(force=True)
        for cal in self._calendars:
            if cal.id == calendar_id:
                return cal
        raise NotFound(
            f"No calendar with id {calendar_id!r}. Call list_calendars to see "
            "the available calendar ids."
        )

    async def default_calendar(self) -> Calendar:
        """The calendar to write to when the caller does not name one.

        ``CALENDAR_MCP_DEFAULT_CALENDAR`` names it, by id or display name.
        Setting it is strongly recommended: iCloud does not publish
        ``schedule-default-calendar-URL`` (it comes back empty), so with nothing
        configured the only available tie-break is the order the server happens
        to list collections in -- which silently changes the moment a calendar
        is added to the account.
        """
        writable = [cal for cal in await self.calendars() if not cal.read_only]
        if not writable:
            raise CalDavError("The account has no writable event calendar.")

        wanted = os.environ.get("CALENDAR_MCP_DEFAULT_CALENDAR", "").strip()
        if wanted:
            for cal in writable:
                if cal.id == wanted or cal.name.lower() == wanted.lower():
                    return cal
            raise NotFound(
                f"CALENDAR_MCP_DEFAULT_CALENDAR is set to {wanted!r}, but no "
                "writable event calendar has that id or name. Available: "
                + ", ".join(repr(cal.name) for cal in writable)
            )

        logger.info(
            "No CALENDAR_MCP_DEFAULT_CALENDAR set; defaulting to %r. Set it to "
            "pin this, since server ordering is not stable.",
            writable[0].name,
        )
        return writable[0]

    async def identities(self) -> tuple[str, ...]:
        """The account's own mailto: addresses, lowercased.

        Used to recognize which ATTENDEE line is the user's own.
        """
        await self._discover()
        return self._addresses

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def query(
        self,
        calendar: Calendar,
        *,
        start: datetime,
        end: datetime,
        expand: bool = True,
    ) -> list[Resource]:
        """Run a time-ranged ``calendar-query``.

        With ``expand`` the server returns one response per occurrence of a
        recurring event, each carrying its own ``RECURRENCE-ID``.

        There is deliberately no text filter here. RFC 4791 allows a
        ``prop-filter``/``text-match`` alongside a ``time-range``, but iCloud
        *silently ignores* the text match when both are present -- a search for
        one title came back with every event in the window, indistinguishable
        from an unfiltered query. Sending the two in the other order is refused
        outright with a 412. Text matching therefore happens client-side, on the
        parsed events, which also lets it cover descriptions, locations and
        participants the way the Fastmail tools do.
        """
        from .dates import to_utc_stamp  # local import keeps the module cycle-free

        range_start = to_utc_stamp(start)
        range_end = to_utc_stamp(end)

        calendar_data = (
            f'<c:calendar-data><c:expand start="{range_start}" end="{range_end}"/>'
            "</c:calendar-data>"
            if expand
            else "<c:calendar-data/>"
        )

        body = (
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            f"<d:prop><d:getetag/>{calendar_data}</d:prop>"
            '<c:filter><c:comp-filter name="VCALENDAR">'
            '<c:comp-filter name="VEVENT">'
            f'<c:time-range start="{range_start}" end="{range_end}"/>'
            "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
        )

        response = await self._request(
            "REPORT",
            calendar.url,
            body=body,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            expect=(207,),
        )
        return _parse_multistatus(response.text, calendar)

    async def get(self, calendar: Calendar, name: str) -> Resource:
        """Fetch one ``.ics`` resource unexpanded, with its ETag."""
        url = _resource_url(calendar, name)
        response = await self._request("GET", url, expect=(200,))
        return Resource(
            calendar_id=calendar.id,
            name=name,
            url=url,
            etag=response.headers.get("ETag", ""),
            ics=response.text,
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def put(
        self,
        calendar: Calendar,
        name: str,
        ics: str,
        *,
        etag: str | None = None,
        create: bool = False,
    ) -> str:
        """Write a resource, returning the new ETag when the server supplies one.

        ``create`` sends ``If-None-Match: *`` so an accidental UID collision
        fails instead of silently overwriting someone's event. Passing ``etag``
        sends ``If-Match`` so a concurrent edit fails instead of being clobbered.
        """
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if create:
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag

        url = _resource_url(calendar, name)
        response = await self._request(
            "PUT", url, body=ics, headers=headers, expect=(200, 201, 204)
        )
        return response.headers.get("ETag", "")

    async def delete(
        self, calendar: Calendar, name: str, *, etag: str | None = None
    ) -> None:
        headers = {"If-Match": etag} if etag else {}
        await self._request(
            "DELETE",
            _resource_url(calendar, name),
            headers=headers,
            expect=(200, 204),
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _calendar_id(url: str) -> str:
    """The stable id for a calendar: the last path segment of its URL."""
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def _resource_url(calendar: Calendar, name: str) -> str:
    return calendar.url.rstrip("/") + "/" + quote(name, safe="")


def _resource_name(url: str) -> str:
    """The decoded last path segment of a resource URL.

    Hrefs come back percent-encoded, and plenty of real resources are named
    after a UID containing an ``@`` (anything imported from Google is). Storing
    the decoded name keeps the event id readable-ish and, more importantly,
    keeps _resource_url from encoding an already-encoded name a second time.
    """
    return unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])


def _normalize_color(value: str | None) -> str:
    """Apple reports ``#RRGGBBAA``; the tool surface reports ``#RRGGBB``."""
    if not value:
        return ""
    value = value.strip()
    if len(value) == 9 and value.startswith("#"):
        return value[:7]
    return value


def _parse_multistatus(xml: str, calendar: Calendar) -> list[Resource]:
    tree = ET.fromstring(xml)
    out: list[Resource] = []
    for response in tree.findall(f"{DAV}response"):
        href_el = response.find(f"{DAV}href")
        data_el = response.find(f".//{CAL}calendar-data")
        if href_el is None or not href_el.text or data_el is None or not data_el.text:
            continue
        etag_el = response.find(f".//{DAV}getetag")
        url = urljoin(calendar.url, href_el.text)
        out.append(
            Resource(
                calendar_id=calendar.id,
                name=_resource_name(url),
                url=url,
                etag=(etag_el.text or "") if etag_el is not None else "",
                ics=data_el.text,
            )
        )
    return out


def client_from_env() -> CalDavClient:
    """Build the account client from the environment.

    ``APPLE_ID`` and ``APPLE_APP_PASSWORD`` match the names the proof-of-concept
    script used, so an existing ``.env`` keeps working.
    """
    apple_id = os.environ.get("APPLE_ID", "").strip()
    password = os.environ.get("APPLE_APP_PASSWORD", "").strip()
    if not apple_id or not password:
        raise ValueError(
            "APPLE_ID and APPLE_APP_PASSWORD must be set. The password is an "
            "app-specific password generated at appleid.apple.com, not the "
            "Apple ID account password."
        )
    return CalDavClient(
        username=apple_id,
        password=password,
        root=os.environ.get("CALENDAR_MCP_CALDAV_ROOT", DEFAULT_ROOT).strip()
        or DEFAULT_ROOT,
    )
