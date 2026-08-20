"""Translation between vCards and the dicts the contact tools return.

The dict shape mirrors the Fastmail contact tools -- `name`, `emails`, `phones`,
`organization`, `jobTitle`, `addresses`, `birthday`, `notes`, `urls` -- so a
model that has learned one surface can drive the other.

Three things about Apple's vCards drive the design here, all observed on a real
account rather than taken from the spec:

* **Cards carry far more than the tool surface models.** A real contact had
  ``X-SOCIALPROFILE`` (twitter, linkedin, gamecenter), ``X-ABRELATEDNAMES``
  (Brother, Father), ``PHOTO``, ``X-ADDRESSBOOKSERVER-PHONEME-DATA`` and more.
  So **updates mutate the existing card in place and never rebuild it** --
  rebuilding from a parsed dict would silently delete every one of those.
* **Labels live in a companion property.** Apple groups properties as
  ``item1.EMAIL`` + ``item1.X-ABLabel``, and the label is sometimes wrapped in
  a ``_$!<Work>!$_`` sigil and sometimes raw text (``Google Voice``).
* **``type=pref`` marks the preferred value**, written as repeated ``type=``
  parameters rather than a comma-separated list. Preferred entries are returned
  first so that ``emails[0]`` is the right address rather than an arbitrary one.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import vobject

from . import ids

logger = logging.getLogger(__name__)

PRODID = "-//dav-mcp//EN"

# Apple wraps its built-in labels like _$!<Work>!$_ but leaves user-defined
# ones bare, so both forms have to be handled.
_APPLE_LABEL = re.compile(r"^_\$!<(.+)>!\$_$")

# Properties that are Apple's internal bookkeeping. They are preserved on write
# but never reported: they are noise to a reader and, in the photo's case,
# enough data to swamp a response.
_NOISE = {
    "PHOTO",
    "PRODID",
    "REV",
    "UID",
    "VERSION",
    "X-ABADR",
    "X-ABLABEL",
    "X-ADDRESSBOOKSERVER-PHONEME-DATA",
    "X-ADDRESSING-GRAMMAR",
    "X-IMAGEHASH",
    "X-IMAGETYPE",
    "X-SHARED-PHOTO-DISPLAY-PREF",
}


class VCardError(ValueError):
    """A card could not be read or built."""


def new_uid() -> str:
    return str(uuid.uuid4()).upper()


def parse(text: str):
    """Parse one vCard, or raise ``VCardError``."""
    try:
        card = vobject.readOne(text)
    except Exception as exc:
        raise VCardError(f"Could not parse the contact data: {exc}") from exc
    if card.name.upper() != "VCARD":
        raise VCardError("The resource is not a vCard.")
    return card


def _type_label(prop) -> str:
    """A label derived from TYPE parameters, ignoring the uninformative ones."""
    types = [t for t in prop.params.get("TYPE", []) if t.lower() != "pref"]
    # INTERNET and VOICE say nothing a person wants to read.
    types = [t for t in types if t.upper() not in ("INTERNET", "VOICE")]
    return types[0].title() if types else ""


def _label_of(card, prop) -> str:
    """The human label for a property.

    Apple groups a property with a companion ``itemN.X-ABLabel`` holding the
    label, wrapped in a ``_$!<Work>!$_`` sigil for its built-ins and left bare
    for user-defined ones. But not every grouped property has a label: a real
    card had ``item4.ADR`` grouped only with ``item4.X-ABADR`` (a country
    code), so a group with no label must still fall back to TYPE rather than
    returning nothing.
    """
    if getattr(prop, "group", None):
        for other in card.getChildren():
            if other.group == prop.group and other.name.upper() == "X-ABLABEL":
                raw = str(other.value).strip()
                match = _APPLE_LABEL.match(raw)
                return match.group(1) if match else raw
    return _type_label(prop)


def _is_preferred(prop) -> bool:
    return any(t.lower() == "pref" for t in prop.params.get("TYPE", []))


def _ordered(card, name: str) -> list[Any]:
    """Every instance of a property, preferred first, original order otherwise."""
    props = card.contents.get(name.lower(), [])
    return sorted(props, key=lambda p: not _is_preferred(p))


def _values(card, name: str) -> list[str]:
    out, seen = [], set()
    for prop in _ordered(card, name):
        value = str(prop.value).strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            out.append(value)
    return out


def _labeled(card, name: str) -> list[dict[str, Any]]:
    out = []
    for prop in _ordered(card, name):
        value = str(prop.value).strip()
        if not value:
            continue
        entry: dict[str, Any] = {"value": value}
        label = _label_of(card, prop)
        if label:
            entry["label"] = label
        if _is_preferred(prop):
            entry["preferred"] = True
        out.append(entry)
    return out


def _addresses(card) -> list[dict[str, Any]]:
    """Postal addresses, both structured and as one formatted line.

    Both shapes are returned deliberately: a mailing label or a form wants the
    components, while an event location or a map link wants a single string.
    """
    out = []
    for prop in _ordered(card, "adr"):
        value = prop.value
        parts = {
            "street": (value.street or "").strip(),
            "city": (value.city or "").strip(),
            "region": (value.region or "").strip(),
            "postcode": (value.code or "").strip(),
            "country": (value.country or "").strip(),
        }
        if not any(parts.values()):
            continue
        # A street can be genuinely multi-line -- vCard escapes it as \n and a
        # real card had one. The structured `street` keeps the break, because a
        # mailing label wants it; `formatted` must not, because it is meant to
        # be pasted into a single field such as an event location.
        line = ", ".join(
            piece
            for piece in (
                " ".join(parts["street"].split()),
                parts["city"],
                " ".join(p for p in (parts["region"], parts["postcode"]) if p),
                parts["country"],
            )
            if piece
        )
        entry = {**parts, "formatted": line}
        label = _label_of(card, prop)
        if label:
            entry["label"] = label
        if _is_preferred(prop):
            entry["preferred"] = True
        out.append(entry)
    return out


def _single(card, name: str) -> str:
    props = card.contents.get(name.lower(), [])
    return str(props[0].value).strip() if props else ""


def to_dict(card, *, book_id: str, resource_name: str) -> dict[str, Any]:
    """Render a vCard as the tool-facing dict."""
    kind = "individual"
    if card.contents.get("x-addressbookserver-kind"):
        if str(card.contents["x-addressbookserver-kind"][0].value).lower() == "group":
            kind = "group"

    organization = ""
    if card.contents.get("org"):
        value = card.contents["org"][0].value
        parts = value if isinstance(value, list) else [value]
        organization = ", ".join(str(p).strip() for p in parts if str(p).strip())

    payload: dict[str, Any] = {
        "id": ids.encode(book_id, resource_name),
        "kind": kind,
        "name": _single(card, "fn"),
        "emails": _values(card, "email"),
        "phones": _values(card, "tel"),
        "organization": organization,
        "jobTitle": _single(card, "title"),
        "addresses": _addresses(card),
        "birthday": _single(card, "bday"),
        "notes": _single(card, "note"),
        "urls": _values(card, "url"),
    }

    # Labels are genuinely useful ("which of these is his work address?") but
    # would clutter the common case, so they ride alongside rather than
    # replacing the plain lists Fastmail returns.
    detail = {
        "emails": _labeled(card, "email"),
        "phones": _labeled(card, "tel"),
        "urls": _labeled(card, "url"),
    }
    if any(any(e.get("label") for e in v) for v in detail.values()):
        payload["labeled"] = {k: v for k, v in detail.items() if v}

    extras = _extras(card)
    if extras:
        payload["other"] = extras

    return payload


def _extras(card) -> dict[str, list[str]]:
    """Anything on the card the tool surface does not model, as readable text.

    Reported rather than dropped: a card carrying "Brother: Travis Fields" or a
    LinkedIn profile is answering a question somebody might well ask, and the
    alternative is pretending the data is not there.
    """
    modelled = {
        "FN", "N", "EMAIL", "TEL", "ADR", "ORG", "TITLE", "BDAY", "NOTE", "URL",
        "X-ADDRESSBOOKSERVER-KIND", "X-ADDRESSBOOKSERVER-MEMBER",
    }
    out: dict[str, list[str]] = {}
    for prop in card.getChildren():
        name = prop.name.upper()
        if name in modelled or name in _NOISE:
            continue
        value = str(prop.value).strip()
        if not value:
            continue
        label = _label_of(card, prop)
        pretty = name.removeprefix("X-").replace("-", " ").title()
        key = label or pretty
        out.setdefault(key, []).append(value)
    return out


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------


def build(
    *,
    uid: str,
    name: str,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    organization: str = "",
    birthday: str = "",
    notes: str = "",
) -> str:
    """Construct a fresh vCard 3.0, the version Apple's own clients write."""
    card = vobject.vCard()
    card.add("prodid").value = PRODID
    card.add("uid").value = uid
    card.add("fn").value = name

    # N is required in vCard 3.0. Apple writes "Last;First;;;" -- a best-effort
    # split is better than an empty structured name, which some clients render
    # as a blank entry in their contact list.
    n = card.add("n")
    bits = name.split()
    n.value = vobject.vcard.Name(
        family=bits[-1] if len(bits) > 1 else "",
        given=bits[0] if bits else "",
    )

    for index, email in enumerate(emails or []):
        prop = card.add("email")
        prop.value = email
        prop.type_paramlist = ["INTERNET", "pref"] if index == 0 else ["INTERNET"]

    for index, phone in enumerate(phones or []):
        prop = card.add("tel")
        prop.value = phone
        prop.type_paramlist = ["CELL", "VOICE", "pref"] if index == 0 else ["CELL", "VOICE"]

    if organization:
        card.add("org").value = [organization]
    if birthday:
        card.add("bday").value = birthday
    if notes:
        card.add("note").value = notes

    return card.serialize()


def set_text(card, name: str, value: str) -> None:
    """Replace a single-valued text property, removing it when cleared."""
    key = name.lower()
    if key in card.contents:
        del card.contents[key]
    if value:
        card.add(key).value = value


def set_org(card, organization: str) -> None:
    if "org" in card.contents:
        del card.contents["org"]
    if organization:
        card.add("org").value = [organization]


def add_values(card, name: str, values: list[str], type_params: list[str]) -> list[str]:
    """Add values that are not already present. Returns the ones actually added."""
    key = name.lower()
    existing = {str(p.value).strip().lower() for p in card.contents.get(key, [])}
    added = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned.lower() in existing:
            continue
        existing.add(cleaned.lower())
        prop = card.add(key)
        prop.value = cleaned
        prop.type_paramlist = list(type_params)
        added.append(cleaned)
    return added


def remove_values(card, name: str, values: list[str]) -> list[str]:
    """Remove values by case-insensitive match. Returns the ones removed.

    Also drops the companion ``X-ABLabel`` of anything grouped, so removing an
    email does not leave an orphaned label behind on the card.
    """
    key = name.lower()
    targets = {v.strip().lower() for v in values if v.strip()}
    if not targets or key not in card.contents:
        return []

    removed, kept, orphaned_groups = [], [], set()
    for prop in card.contents[key]:
        if str(prop.value).strip().lower() in targets:
            removed.append(str(prop.value).strip())
            if getattr(prop, "group", None):
                orphaned_groups.add(prop.group)
        else:
            kept.append(prop)

    if kept:
        card.contents[key] = kept
    else:
        del card.contents[key]

    for group in orphaned_groups:
        for other_key, props in list(card.contents.items()):
            survivors = [
                p
                for p in props
                if not (getattr(p, "group", None) == group and p.name.upper() == "X-ABLABEL")
            ]
            if len(survivors) != len(props):
                if survivors:
                    card.contents[other_key] = survivors
                else:
                    del card.contents[other_key]

    return removed


def touch(card) -> None:
    """Stamp REV, the way every other CardDAV client does."""
    from datetime import datetime, timezone

    if "rev" in card.contents:
        del card.contents["rev"]
    card.add("rev").value = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "VCardError",
    "add_values",
    "build",
    "new_uid",
    "parse",
    "remove_values",
    "set_org",
    "set_text",
    "to_dict",
    "touch",
]
