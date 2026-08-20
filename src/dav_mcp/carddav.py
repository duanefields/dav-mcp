"""The CardDAV client: address book discovery, search, and writes.

What iCloud actually answered when probed:

* Discovery is the same walk as CalDAV -- ``current-user-principal`` ->
  ``addressbook-home-set`` -> collections -- with the same credentials against
  ``contacts.icloud.com``.
* The address book has **no ``displayname``**, so callers need a fallback.
* **``addressbook-query`` works, even though the server does not advertise it.**
  ``supported-report-set`` lists only ``addressbook-multiget`` and
  ``sync-collection``, yet a ``prop-filter`` REPORT returned 1 match against an
  address book of 913 cards. Trusting the advertisement would have meant
  fetching every card to filter locally, for nothing.

That last point is the mirror image of the CalDAV trap, where a filter *was*
advertised and then silently ignored. The rule for iCloud is to test the
behavior rather than believe the advertisement, in either direction.
"""

from __future__ import annotations

import logging
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin

from .dav import CARD, DAV
from .dav import DISCOVERY_TTL as _DISCOVERY_TTL
from .dav import (
    AuthError,
    Conflict,
    DavClient,
    DavError,
    NotFound,
    Throttled,
)
from .dav import collection_id as _book_id
from .dav import is_writable
from .dav import credentials_from_env
from .dav import resource_name as _resource_name
from .dav import resource_url as _resource_url

logger = logging.getLogger(__name__)

DEFAULT_ROOT = "https://contacts.icloud.com"

CardDavError = DavError

# Fields a person would expect a search to look at. Apple accepts a filter on
# each of these; see `search` for why they are issued as one anyof filter.
SEARCH_PROPERTIES = ("FN", "N", "EMAIL", "TEL", "ORG", "NICKNAME", "NOTE")

_PROPFIND_HOME = (
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><c:addressbook-home-set/></d:prop></d:propfind>"
)

_PROPFIND_BOOKS = (
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    "<d:prop><d:displayname/><d:resourcetype/><d:current-user-privilege-set/>"
    "</d:prop></d:propfind>"
)

__all__ = [
    "AddressBook",
    "AuthError",
    "Card",
    "CardDavClient",
    "CardDavError",
    "Conflict",
    "DEFAULT_ROOT",
    "NotFound",
    "Throttled",
    "client_from_env",
]


@dataclass(frozen=True)
class AddressBook:
    id: str
    name: str
    url: str
    read_only: bool


@dataclass(frozen=True)
class Card:
    """One ``.vcf`` resource."""

    book_id: str
    name: str
    url: str
    etag: str
    vcard: str


class CardDavClient(DavClient):
    """A thin async CardDAV client scoped to a single account."""

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
        self._books: list[AddressBook] = []
        self._discovered_at = 0.0

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def _discover(self, *, force: bool = False) -> None:
        if not force and self._home and time.time() - self._discovered_at < _DISCOVERY_TTL:
            return

        principal = await self._principal()
        self._home = await self._home_set(
            principal, _PROPFIND_HOME, f"{CARD}addressbook-home-set"
        )
        self._books = await self._read_books(self._home)
        self._discovered_at = time.time()
        logger.info("Discovered %d address book(s) under %s", len(self._books), self._home)

    async def _read_books(self, home: str) -> list[AddressBook]:
        tree = await self._propfind(home, _PROPFIND_BOOKS, depth="1")
        found: list[AddressBook] = []

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
            if f"{CARD}addressbook" not in kinds:
                continue

            name_el = response.find(f".//{DAV}displayname")
            name = (name_el.text or "").strip() if name_el is not None else ""
            privileges = {
                priv.tag
                for priv in response.findall(
                    f".//{DAV}current-user-privilege-set//{DAV}privilege/*"
                )
            }
            book_id = _book_id(url)
            found.append(
                AddressBook(
                    id=book_id,
                    # iCloud returns no displayname for its address book, so a
                    # fallback is required rather than showing an empty name.
                    name=name or "Contacts",
                    url=url,
                    read_only=not is_writable(privileges),
                )
            )

        return found

    async def address_books(self) -> list[AddressBook]:
        await self._discover()
        return list(self._books)

    async def address_book(self, book_id: str) -> AddressBook:
        for book in await self.address_books():
            if book.id == book_id:
                return book
        await self._discover(force=True)
        for book in self._books:
            if book.id == book_id:
                return book
        raise NotFound(
            f"No address book with id {book_id!r}. Call list_address_books to "
            "see the available ids."
        )

    async def default_book(self) -> AddressBook:
        """The address book to write to when the caller does not name one."""
        writable = [book for book in await self.address_books() if not book.read_only]
        if not writable:
            raise CardDavError("The account has no writable address book.")
        return writable[0]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def search(self, book: AddressBook, text: str | None = None) -> list[Card]:
        """Run an ``addressbook-query``, optionally filtered by text.

        The filter is issued as a single ``anyof`` across every property a
        person would expect a search to consider, so one round trip covers
        names, addresses, numbers and organizations. Without ``text`` this
        returns the whole book, which is 900+ cards on a real account -- always
        pass a query for anything interactive.
        """
        if text:
            escaped = _xml_escape(text)
            filters = "".join(
                f'<c:prop-filter name="{prop}">'
                f'<c:text-match collation="i;unicode-casemap" match-type="contains">'
                f"{escaped}</c:text-match></c:prop-filter>"
                for prop in SEARCH_PROPERTIES
            )
            body = (
                '<c:addressbook-query xmlns:d="DAV:" '
                'xmlns:c="urn:ietf:params:xml:ns:carddav">'
                "<d:prop><d:getetag/><c:address-data/></d:prop>"
                f'<c:filter test="anyof">{filters}</c:filter>'
                "</c:addressbook-query>"
            )
        else:
            body = (
                '<c:addressbook-query xmlns:d="DAV:" '
                'xmlns:c="urn:ietf:params:xml:ns:carddav">'
                "<d:prop><d:getetag/><c:address-data/></d:prop>"
                "<c:filter/></c:addressbook-query>"
            )

        response = await self._request(
            "REPORT",
            book.url,
            body=body,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            expect=(207,),
        )
        return _parse_multistatus(response.text, book)

    async def get(self, book: AddressBook, name: str) -> Card:
        url = _resource_url(book.url, name)
        etag, text = await self._get(url)
        return Card(book_id=book.id, name=name, url=url, etag=etag, vcard=text)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def put(
        self,
        book: AddressBook,
        name: str,
        vcard: str,
        *,
        etag: str | None = None,
        create: bool = False,
    ) -> str:
        return await self._put(
            _resource_url(book.url, name),
            vcard,
            "text/vcard; charset=utf-8",
            etag=etag,
            create=create,
        )

    async def delete(
        self, book: AddressBook, name: str, *, etag: str | None = None
    ) -> None:
        await self._delete(_resource_url(book.url, name), etag=etag)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _parse_multistatus(xml: str, book: AddressBook) -> list[Card]:
    tree = ET.fromstring(xml)
    out: list[Card] = []
    for response in tree.findall(f"{DAV}response"):
        href_el = response.find(f"{DAV}href")
        data_el = response.find(f".//{CARD}address-data")
        if href_el is None or not href_el.text or data_el is None or not data_el.text:
            continue
        etag_el = response.find(f".//{DAV}getetag")
        url = urljoin(book.url, href_el.text)
        out.append(
            Card(
                book_id=book.id,
                name=_resource_name(url),
                url=url,
                etag=(etag_el.text or "") if etag_el is not None else "",
                vcard=data_el.text,
            )
        )
    return out


def client_from_env() -> CardDavClient:
    """Build the CardDAV client from the environment.

    Deliberately the same credentials as the CalDAV client: one Apple ID, one
    app-specific password, both services.
    """
    apple_id, password = credentials_from_env()
    return CardDavClient(
        username=apple_id,
        password=password,
        root=os.environ.get("DAV_MCP_CARDDAV_ROOT", DEFAULT_ROOT).strip()
        or DEFAULT_ROOT,
    )
