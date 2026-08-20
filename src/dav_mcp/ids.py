"""Opaque, stateless event ids.

An id has to survive a round trip through a model and come back naming exactly
one occurrence of one resource on one calendar. It encodes the calendar id, the
resource name and an optional recurrence key, so nothing needs to be stored
server-side -- there is no database and no id table to fall out of sync with the
calendar.

The three parts are joined with US (0x1f) rather than a printable separator.
Resource names are not tame: anything imported from Google is named
``<uid>@google.com.ics``, and a "/" or "@" separator splits such a name in the
middle. A control character cannot occur in either a resource name or a
recurrence key, so the split is unambiguous by construction.

The encoding is base64url purely so the result survives being quoted, pasted and
JSON-encoded without a slash or a dot causing trouble. It is not a secret.
"""

from __future__ import annotations

import base64
import binascii


class BadId(ValueError):
    """The id was not produced by this server."""


# The calendar tools were written first and named the exception after events.
BadEventId = BadId


SEP = "\x1f"

_MESSAGE = (
    "is not a valid {kind} id. {Kind} ids come from {source}; pass one through "
    "unchanged rather than composing it by hand."
)


def _message(kind: str) -> str:
    source = "search_contacts" if kind == "contact" else "search_events"
    return _MESSAGE.format(kind=kind, Kind=kind.title(), source=source)


def encode(calendar_id: str, resource_name: str, recurrence_id: str = "") -> str:
    if not calendar_id or not resource_name:
        raise ValueError("calendar_id and resource_name are both required")
    parts = [calendar_id, resource_name]
    if recurrence_id:
        parts.append(recurrence_id)
    raw = SEP.join(parts)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode(event_id: str, kind: str = "event") -> tuple[str, str, str]:
    """Return ``(calendar_id, resource_name, recurrence_id)``.

    ``recurrence_id`` is an empty string for a non-recurring event or for the
    master of a series, and always for a contact.

    ``kind`` only shapes the error message, so a model handed a bad contact id
    is pointed at ``search_contacts`` rather than at ``search_events``.
    """
    if not event_id or not event_id.strip():
        raise BadId(f"An empty {kind} id {_message(kind)}")

    text = event_id.strip()
    padding = "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(text + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BadId(f"{event_id!r} {_message(kind)}") from None

    parts = raw.split(SEP)
    if len(parts) == 2:
        parts.append("")
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise BadId(f"{event_id!r} {_message(kind)}")

    calendar_id, resource_name, recurrence_id = parts
    return calendar_id, resource_name, recurrence_id
