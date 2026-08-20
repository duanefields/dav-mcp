from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin

from .dav import APPLE as ICAL_NS
from .dav import CAL, DAV
from .dav import DISCOVERY_TTL as _DISCOVERY_TTL
from .dav import (
    AuthError,
    Conflict,
    DavClient,
    DavError,
    NotFound,
    Throttled,
)
from .dav import collection_id as _calendar_id
from .dav import credentials_from_env, is_writable
from .dav import resource_name as _resource_name
from .dav import resource_url as _resource_url

logger = logging.getLogger(__name__)

DEFAULT_ROOT = "https://caldav.icloud.com"

# The HTTP plumbing -- auth, retries, throttling, error mapping -- lives in
# dav.py, shared with the CardDAV client. Both speak to the same account and
# hit the same walls, so a fix to either applies to both.
CalDavError = DavError

__all__ = [
    "AuthError",
    "CalDavClient",
    "CalDavError",
    "Calendar",
    "Conflict",
    "DEFAULT_ROOT",
    "NotFound",
    "Resource",
    "Throttled",
    "client_from_env",
]

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


class CalDavClient(DavClient):
    """A thin async CalDAV client scoped to a single account."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        root: str = DEFAULT_ROOT,
        timeout: float = 30.0,
    ):
        super().__init__(
            username=username, password=password, root=root, timeout=timeout
        )
        self._home: str | None = None
        self._addresses: tuple[str, ...] = ()
        self._calendars: list[Calendar] = []
        self._discovered_at = 0.0

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def _discover(self, *, force: bool = False) -> None:
        if not force and self._home and time.time() - self._discovered_at < _DISCOVERY_TTL:
            return

        principal = await self._principal()
        self._home = await self._home_set(
            principal, _PROPFIND_HOME, f"{CAL}calendar-home-set"
        )

        tree = await self._propfind(principal, _PROPFIND_HOME)
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
                    read_only=not is_writable(privileges),
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
        url = _resource_url(calendar.url, name)
        etag, text = await self._get(url)
        return Resource(
            calendar_id=calendar.id, name=name, url=url, etag=etag, ics=text
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
        return await self._put(
            _resource_url(calendar.url, name),
            ics,
            "text/calendar; charset=utf-8",
            etag=etag,
            create=create,
        )

    async def delete(
        self, calendar: Calendar, name: str, *, etag: str | None = None
    ) -> None:
        await self._delete(_resource_url(calendar.url, name), etag=etag)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


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
    """Build the CalDAV client from the environment."""
    import os

    apple_id, password = credentials_from_env()
    return CalDavClient(
        username=apple_id,
        password=password,
        root=os.environ.get("CALENDAR_MCP_CALDAV_ROOT", DEFAULT_ROOT).strip()
        or DEFAULT_ROOT,
    )
