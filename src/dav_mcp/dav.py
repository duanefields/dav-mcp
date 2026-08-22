"""Shared WebDAV plumbing for the CalDAV and CardDAV clients.

Both talk to the same account on the same provider with the same credentials,
so they hit the same walls: the same auth failures, the same account-wide rate
limiting, and the same principal-then-home discovery walk. Keeping that in one
place means a fix to any of it applies to both, rather than being fixed in one
protocol and quietly missing from the other.

Protocol-specific work -- what a collection is, what a resource contains, which
REPORTs exist -- stays in ``caldav.py`` and ``carddav.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "dav-mcp/0.1"
DISCOVERY_TTL = 300.0

DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
CARD = "{urn:ietf:params:xml:ns:carddav}"
APPLE = "{http://apple.com/ns/ical/}"

# iCloud starts answering 503 after a burst of writes, and the throttling is
# account-wide: caldav.icloud.com and contacts.icloud.com both refuse, reads
# included, for tens of minutes. A short retry turns a transient blip into a
# slow success.
#
# Deliberately brief. iCloud sends `Retry-After: 30` when it is throttling in
# earnest, and honoring that literally makes a tool call hang for a minute and a
# half -- far worse than failing in a couple of seconds with a message saying to
# try again. Retry the blips; report the real throttling.
RETRY_STATUSES = (429, 503)
RETRY_BACKOFF = (1.0, 3.0)
RETRY_AFTER_CAP = 5.0

PROPFIND_PRINCIPAL = (
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/>'
    "</d:prop></d:propfind>"
)


class DavError(Exception):
    """A request failed in a way the caller should surface."""


class NotFound(DavError):
    """The requested resource does not exist on the server."""


class Conflict(DavError):
    """The resource changed underneath us, or already existed when creating."""


class AuthError(DavError):
    """The Apple ID or app-specific password was rejected."""


class Throttled(DavError):
    """iCloud is refusing requests for now. Not a credential problem."""


class DavClient:
    """HTTP plumbing shared by the CalDAV and CardDAV clients."""

    def __init__(
        self,
        *,
        username: str,
        password: str,
        root: str,
        timeout: float = 30.0,
    ):
        if not username or not password:
            raise ValueError("username and password are both required")
        self._root = root.rstrip("/")
        self._auth = httpx.BasicAuth(username, password)
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

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
        for attempt, pause in enumerate((*RETRY_BACKOFF, None)):
            try:
                response = await self._http().request(
                    method,
                    url,
                    content=body.encode("utf-8") if body else None,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise DavError(f"{method} {url} failed: {exc}") from exc

            if response.status_code not in RETRY_STATUSES or pause is None:
                break

            # Honor Retry-After when iCloud bothers to send one, within reason.
            delay = pause
            retry_after = response.headers.get("Retry-After", "").strip()
            if retry_after.isdigit():
                delay = max(delay, min(float(retry_after), RETRY_AFTER_CAP))
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

        if response.status_code in RETRY_STATUSES:
            raise Throttled(
                f"iCloud is temporarily refusing requests ({response.status_code}) "
                "and did not recover after several retries. This is rate limiting, "
                "not a credential problem -- it usually clears within a few "
                "minutes, and it affects the whole Apple ID rather than one "
                "service. Wait and try again."
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
                "the resource changed on the server since it was read."
            )
        if response.status_code not in expect:
            raise DavError(
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

    async def _principal(self) -> str:
        tree = await self._propfind(self._root + "/", PROPFIND_PRINCIPAL)
        href = tree.find(f".//{DAV}current-user-principal/{DAV}href")
        if href is None or not href.text:
            raise DavError(
                "The server did not return a current-user-principal. This usually "
                "means the Apple ID or app-specific password is wrong."
            )
        return self._resolve(self._root + "/", href.text)

    async def _home_set(self, principal: str, body: str, tag: str) -> str:
        """Resolve a home-set href, e.g. calendar-home-set or addressbook-home-set."""
        tree = await self._propfind(principal, body)
        href = tree.find(f".//{tag}/{DAV}href")
        if href is None or not href.text:
            raise DavError(f"The server did not return {tag}.")
        return self._resolve(principal, href.text)

    def _resolve(self, base: str, href: str) -> str:
        """Join a server-supplied href against ``base``, keeping it in the account.

        Discovery follows hrefs out of the response body and then sends the next
        request -- with the account's credentials attached -- to whatever they
        name. An absolute href pointing somewhere else would hand the Apple ID
        and app-specific password to that host. httpx already strips
        ``Authorization`` across a redirect; this closes the same hole for the
        hrefs, which are ordinary new requests it cannot see.

        The check is containment rather than same-origin, because iCloud
        genuinely moves the account across hosts: the principal comes back on
        ``caldav.icloud.com`` and the home-set on ``p64-caldav.icloud.com``.
        Anything under the root's parent domain is therefore allowed. That is a
        guardrail against a hostile response or a mistyped ``DAV_MCP_CALDAV_ROOT``,
        not a defense against someone who can already forge TLS for the root --
        they have the credentials from the first request regardless.
        """
        target = urljoin(base, href)
        if not _within(self._root, target):
            raise DavError(
                f"The server pointed discovery at {urlparse(target).netloc or target!r}, "
                f"which is outside {urlparse(self._root).netloc}. Refusing to send "
                "the account credentials there."
            )
        return target

    # ------------------------------------------------------------------
    # Resource-level operations, identical for both protocols
    # ------------------------------------------------------------------

    async def _get(self, url: str) -> tuple[str, str]:
        """Fetch a resource. Returns ``(etag, body)``."""
        response = await self._request("GET", url, expect=(200,))
        return response.headers.get("ETag", ""), response.text

    async def _put(
        self,
        url: str,
        body: str,
        content_type: str,
        *,
        etag: str | None = None,
        create: bool = False,
    ) -> str:
        """Write a resource, returning the new ETag when the server supplies one.

        ``create`` sends ``If-None-Match: *`` so an accidental UID collision
        fails instead of silently overwriting. Passing ``etag`` sends
        ``If-Match`` so a concurrent edit fails instead of being clobbered.
        """
        headers = {"Content-Type": content_type}
        if create:
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag
        response = await self._request(
            "PUT", url, body=body, headers=headers, expect=(200, 201, 204)
        )
        return response.headers.get("ETag", "")

    async def _delete(self, url: str, *, etag: str | None = None) -> None:
        headers = {"If-Match": etag} if etag else {}
        await self._request("DELETE", url, headers=headers, expect=(200, 204))


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _within(root: str, target: str) -> bool:
    """Whether ``target`` is the same host as ``root`` or a sibling under it.

    Same scheme, and the host must equal the root's or sit under the root's
    parent domain -- ``caldav.icloud.com`` admits ``p64-caldav.icloud.com``
    but not ``icloud.com.example.net``.
    """
    base, other = urlparse(root), urlparse(target)
    if other.scheme != base.scheme or not other.hostname or not base.hostname:
        return False

    host, root_host = other.hostname.lower(), base.hostname.lower()
    if host == root_host:
        return True

    parent = root_host.partition(".")[2]
    # A bare or two-label root has no parent worth widening to; "example.com"
    # must not admit every host under "com".
    if parent.count(".") < 1:
        return False
    return host == parent or host.endswith("." + parent)


def is_writable(privileges: set[str]) -> bool:
    """Whether a collection accepts writes, per its current-user-privilege-set.

    The two protocols report this differently on iCloud: calendars come back
    with ``write-content``, address books with plain ``write``. Checking only
    one marks the other read-only and refuses every write on it, so both count.

    An empty set means the server declined to say. Treat that as writable and
    let the server refuse the PUT: guessing read-only from silence disables
    writing entirely, and iCloud omits plenty of properties it does support.
    """
    if not privileges:
        return True
    names = {tag.rsplit("}", 1)[-1] for tag in privileges}
    return bool(names & {"write", "write-content", "all"})


def collection_id(url: str) -> str:
    """The stable id for a collection: the last path segment of its URL."""
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def resource_url(collection_url: str, name: str) -> str:
    return collection_url.rstrip("/") + "/" + quote(name, safe="")


def resource_name(url: str) -> str:
    """The decoded last path segment of a resource URL.

    Hrefs come back percent-encoded, and plenty of real resources are named
    after a UID containing an ``@`` (anything imported from Google is). Storing
    the decoded name keeps ids readable-ish and, more importantly, keeps
    ``resource_url`` from encoding an already-encoded name a second time.
    """
    return unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1])


def credentials_from_env() -> tuple[str, str]:
    """The Apple ID and app-specific password, shared by both protocols."""
    apple_id = os.environ.get("APPLE_ID", "").strip()
    password = os.environ.get("APPLE_APP_PASSWORD", "").strip()
    if not apple_id or not password:
        raise ValueError(
            "APPLE_ID and APPLE_APP_PASSWORD must be set. The password is an "
            "app-specific password generated at appleid.apple.com, not the "
            "Apple ID account password."
        )
    return apple_id, password
