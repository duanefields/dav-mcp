"""Translation between iCalendar VEVENTs and the dicts the tools return.

The dict shape deliberately mirrors the Fastmail calendar tools, so a model that
has learned one surface can drive the other without relearning field names:
``title``, ``start``, ``duration``, ``timeZone``, ``isAllDay``, ``locations``,
``participants``, ``recurrence``.

Two asymmetries between the two worlds are worth knowing about:

* iCalendar expresses length as ``DTEND`` (or a ``DURATION``); the tool surface
  expresses it as ``duration``. Conversion happens here, at the boundary.
* An all-day event is date-valued and floating -- it has no time of day and no
  zone. ``DTSTART;VALUE=DATE`` is the marker, and it is what keeps a birthday
  from sliding a day when read from another time zone.
"""

from __future__ import annotations

import logging
import uuid
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo

from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent
from icalendar import Timezone as ITimezone
from icalendar import vCalAddress, vText

from . import ids
from .dates import DateError, format_duration, parse_duration

logger = logging.getLogger(__name__)

PRODID = "-//calendar-mcp//EN"

_FREQUENCIES = ("daily", "weekly", "monthly", "yearly")
_WEEKDAYS = ("mo", "tu", "we", "th", "fr", "sa", "su")

_PARTSTAT = {
    "NEEDS-ACTION": "needs-action",
    "ACCEPTED": "accepted",
    "DECLINED": "declined",
    "TENTATIVE": "tentative",
    "DELEGATED": "delegated",
}


class ICalError(ValueError):
    """An event could not be read or built."""


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def parse_resource(ics: str) -> list[IEvent]:
    """Every VEVENT in a resource, master first then any overrides."""
    try:
        calendar = ICalendar.from_ical(ics)
    except Exception as exc:
        raise ICalError(f"Could not parse the calendar data: {exc}") from exc

    events = [cast(IEvent, component) for component in calendar.walk("VEVENT")]
    if not events:
        raise ICalError("The resource contains no VEVENT.")
    # Overrides carry RECURRENCE-ID; the master does not.
    events.sort(key=lambda ev: ev.get("RECURRENCE-ID") is not None)
    return events


def event_to_dict(
    event: IEvent,
    *,
    calendar_id: str,
    resource_name: str,
    calendar_color: str = "",
) -> dict[str, Any]:
    """Render one VEVENT as the tool-facing dict."""
    start_prop = event.get("DTSTART")
    if start_prop is None:
        raise ICalError("The event has no DTSTART.")

    start = start_prop.dt
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    tzid = _tzid_of(start_prop, start)
    length = _length_of(event, start, all_day)

    recurrence_prop = event.get("RECURRENCE-ID")
    recurrence_key_value = recurrence_key(recurrence_prop)

    payload: dict[str, Any] = {
        "id": ids.encode(calendar_id, resource_name, recurrence_key_value),
        "title": _text(event, "SUMMARY"),
        "description": _text(event, "DESCRIPTION"),
        "start": _stamp(start),
        "duration": format_duration(length),
        "timeZone": "" if all_day else tzid,
        "isAllDay": all_day,
        "color": _text(event, "COLOR") or calendar_color,
        "locations": [loc for loc in [_text(event, "LOCATION")] if loc],
        "virtualLocations": _virtual_locations(event),
        "participants": _participants(event),
        "calendarIds": {calendar_id: True},
    }

    if recurrence_prop is not None:
        # Reported as wall time in the event's own zone so it lines up with
        # `start`. iCloud hands RECURRENCE-ID back in UTC, so stamping it raw
        # showed an occurrence as 14:30 next to a start of 09:30.
        payload["recurrenceId"] = _stamp(_in_zone_of(recurrence_prop.dt, start))

    recurrence = _recurrence_to_dict(event)
    if recurrence:
        payload["recurrence"] = recurrence

    status = _text(event, "STATUS")
    if status:
        payload["status"] = status.lower()

    # TRANSP is what an event says about whether it occupies the person, as
    # opposed to merely sitting on the calendar. Availability depends on it,
    # and it is cheap to carry.
    payload["blocksTime"] = _blocks_time(event, status)

    return payload


def _blocks_time(event: IEvent, status: str) -> bool:
    """Whether the event itself claims to occupy the person.

    Two things make an event not count, and both are properties of the event:

    * ``TRANSP:TRANSPARENT`` -- it explicitly declares that it does not occupy
      the person. This is the standard signal.
    * ``STATUS:CANCELLED`` -- it is not happening.

    Two further exclusions are deliberately *not* decided here, because they
    are not properties of the event: whether an all-day event blocks the day is
    policy, and whether the user declined depends on which addresses are
    theirs. Both are the caller's to apply -- see ``server._busy_intervals``.
    """
    if status.upper() == "CANCELLED":
        return False
    if _text(event, "TRANSP").upper() == "TRANSPARENT":
        return False
    return True


def recurrence_key(prop: Any) -> str:
    """The RECURRENCE-ID of an occurrence, in its literal iCalendar form.

    This is what an event id carries, and it is deliberately NOT the pretty
    local-time ``start``. iCloud returns RECURRENCE-ID in UTC
    ("20260901T143000Z") while DTSTART stays in its own zone
    ("20260901T093000" TZID=America/Chicago). Rendering both through the same
    local formatter produced an id that looked like a wall-clock time but held
    the UTC one; writing it back stamped 14:30 America/Chicago onto the
    override, which matches no real occurrence, so the edit silently applied to
    nothing. Keeping the raw form means the value only ever round-trips.
    """
    if prop is None:
        return ""
    return prop.to_ical().decode("ascii")


def recurrence_id_value(key: str, reference: Any) -> Any:
    """Turn a recurrence key back into the value a RECURRENCE-ID needs.

    ``reference`` is the master's DTSTART, consulted only to decide date vs
    date-time; the zone comes from the key itself so nothing is reinterpreted.
    """
    if not key:
        raise ICalError("An occurrence id carried no recurrence key.")

    all_day = not isinstance(reference, datetime)
    if all_day or (len(key) == 8 and key.isdigit()):
        return datetime.strptime(key[:8], "%Y%m%d").date()

    if key.endswith("Z"):
        return datetime.strptime(key, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

    naive = datetime.strptime(key[:15], "%Y%m%dT%H%M%S")
    tzinfo = getattr(reference, "tzinfo", None)
    return naive.replace(tzinfo=tzinfo) if tzinfo else naive


def _text(event: IEvent, name: str) -> str:
    value = event.get(name)
    if value is None:
        return ""
    return str(value).strip()


def _tzid_of(prop: Any, value: Any) -> str:
    """The IANA zone name for a DTSTART, preferring the literal TZID param.

    icalendar resolves the parameter into a tzinfo, but a UTC value arrives with
    no parameter at all, and a floating value with neither -- so both sources
    are consulted.
    """
    tzid = prop.params.get("TZID") if hasattr(prop, "params") else None
    if tzid:
        return str(tzid)
    tzinfo = getattr(value, "tzinfo", None)
    if tzinfo is None:
        return ""
    key = getattr(tzinfo, "key", None)
    if key:
        return str(key)
    return "UTC" if tzinfo is timezone.utc else ""


def _length_of(event: IEvent, start: Any, all_day: bool) -> timedelta:
    """Event length, from DTEND if present, else DURATION, else a default."""
    end_prop = event.get("DTEND")
    if end_prop is not None:
        end = end_prop.dt
        # Both halves must be the same kind. A resource mixing a date-valued
        # DTSTART with a datetime DTEND is malformed, and subtracting the two
        # raises rather than returning something plausible.
        if isinstance(end, datetime) and isinstance(start, datetime):
            return end - start
        if type(end) is date and type(start) is date:
            return timedelta(days=(end - start).days)

    duration_prop = event.get("DURATION")
    if duration_prop is not None:
        return duration_prop.dt

    # RFC 5545: a date-valued event with no end lasts one day; a timed one is
    # instantaneous, but reporting PT0S for it is more honest than guessing.
    return timedelta(days=1) if all_day else timedelta(0)


def _stamp(value: Any) -> str:
    """Format a date or datetime the way the tool surface reports ``start``."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%dT00:00:00")
    return str(value)


def _in_zone_of(value: Any, reference: Any) -> Any:
    """Re-express an aware datetime in the reference's zone, for display only."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return value
    tzinfo = getattr(reference, "tzinfo", None)
    return value.astimezone(tzinfo) if tzinfo is not None else value


def _virtual_locations(event: IEvent) -> list[str]:
    prop = event.get("CONFERENCE")
    if prop is None:
        return []
    values = prop if isinstance(prop, list) else [prop]
    return [str(value).strip() for value in values if str(value).strip()]


def _participants(event: IEvent) -> list[dict[str, Any]]:
    """ATTENDEE lines plus the ORGANIZER, flattened into one list."""
    attendees = event.get("ATTENDEE")
    if attendees is None:
        attendees = []
    elif not isinstance(attendees, list):
        attendees = [attendees]

    organizer = event.get("ORGANIZER")
    organizer_email = _address_email(organizer) if organizer is not None else ""

    people: list[dict[str, Any]] = []
    seen: set[str] = set()

    for attendee in attendees:
        email = _address_email(attendee)
        if email and email.lower() in seen:
            continue
        if email:
            seen.add(email.lower())
        person: dict[str, Any] = {
            "name": str(attendee.params.get("CN", "")),
            "email": email,
            "status": _PARTSTAT.get(
                str(attendee.params.get("PARTSTAT", "")).upper(), ""
            ),
        }
        if organizer_email and email.lower() == organizer_email.lower():
            person["roles"] = {"owner": True}
        people.append(person)

    if organizer_email and organizer_email.lower() not in seen:
        people.append(
            {
                "name": str(organizer.params.get("CN", "")),
                "email": organizer_email,
                "status": "",
                "roles": {"owner": True},
            }
        )

    return people


def _address_email(address: Any) -> str:
    """The bare address from a CAL-ADDRESS value like ``mailto:jo@example.com``."""
    if address is None:
        return ""
    email = str(address.params.get("EMAIL", "")).strip()
    if email:
        return email
    raw = str(address).strip()
    if raw.lower().startswith("mailto:"):
        return raw[7:]
    return raw if "@" in raw else ""


def _recurrence_to_dict(event: IEvent) -> dict[str, Any] | None:
    """Render an RRULE in the shape ``create_event`` accepts.

    Expanded occurrences have no RRULE -- the server strips it -- so this is
    populated only when reading an unexpanded master.
    """
    rule = event.get("RRULE")
    if rule is None:
        return None

    frequency = _first(rule.get("FREQ"))
    if not frequency:
        return None

    out: dict[str, Any] = {"frequency": str(frequency).lower()}

    interval = _first(rule.get("INTERVAL"))
    if interval and int(interval) != 1:
        out["interval"] = int(interval)

    by_day = rule.get("BYDAY")
    if by_day:
        out["byDay"] = [str(day).lower() for day in by_day]

    count = _first(rule.get("COUNT"))
    if count:
        out["count"] = int(count)

    until = _first(rule.get("UNTIL"))
    if until:
        out["until"] = _stamp(until)

    return out


def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------


def new_uid() -> str:
    return str(uuid.uuid4()).upper()


def build_resource(events: list[IEvent], tzids: set[str]) -> str:
    """Wrap VEVENTs in a VCALENDAR, with a VTIMEZONE for every zone they use.

    Omitting VTIMEZONE for a ``TZID`` a resource references is invalid per
    RFC 5545. iCloud tolerates it; other clients reading the same event do not
    reliably, so it is generated rather than skipped.
    """
    calendar = ICalendar()
    calendar.add("PRODID", PRODID)
    calendar.add("VERSION", "2.0")
    calendar.add("CALSCALE", "GREGORIAN")

    for tzid in sorted(tzids):
        if not tzid or tzid == "UTC":
            continue
        try:
            calendar.add_component(_vtimezone(tzid))
        except Exception:
            logger.warning("Could not build a VTIMEZONE for %r; omitting it.", tzid)

    for event in events:
        calendar.add_component(event)

    return calendar.to_ical().decode("utf-8")


@lru_cache(maxsize=64)
def _vtimezone(tzid: str) -> ITimezone:
    """A VTIMEZONE covering the years an event might plausibly touch.

    Unbounded, icalendar emits every transition a zone has ever had -- America/
    Chicago goes back to 1883 and runs to 90 lines, on every event we write.
    Bounding it keeps the resource small. Cached because building one is slow
    and the answer never changes within a run.
    """
    today = date.today()
    return ITimezone.from_tzid(
        tzid,
        first_date=date(today.year - 5, 1, 1),
        last_date=date(today.year + 15, 1, 1),
    )


def build_event(
    *,
    uid: str,
    title: str,
    start: datetime | date,
    duration: timedelta,
    all_day: bool,
    tzid: str,
    description: str = "",
    location: str = "",
    color: str = "",
    recurrence: dict[str, Any] | None = None,
) -> IEvent:
    """Construct a fresh VEVENT."""
    event = IEvent()
    event.add("UID", uid)
    event.add("DTSTAMP", datetime.now(timezone.utc))
    event.add("SEQUENCE", 0)
    event.add("SUMMARY", title)

    _set_timing(event, start=start, duration=duration, all_day=all_day, tzid=tzid)

    if description:
        event.add("DESCRIPTION", description)
    if location:
        event.add("LOCATION", location)
    if color:
        event.add("COLOR", color)
    if recurrence:
        event.add("RRULE", recurrence_to_rrule(recurrence))

    return event


def _set_timing(
    event: IEvent,
    *,
    start: datetime | date,
    duration: timedelta,
    all_day: bool,
    tzid: str,
) -> None:
    """Replace DTSTART/DTEND, dropping any DURATION so the two cannot disagree."""
    for name in ("DTSTART", "DTEND", "DURATION"):
        if name in event:
            del event[name]

    if all_day:
        day = start.date() if isinstance(start, datetime) else start
        days = max(1, round(duration.total_seconds() / 86400))
        event.add("DTSTART", day)
        event.add("DTEND", day + timedelta(days=days))
        return

    if not isinstance(start, datetime):
        start = datetime.combine(start, datetime.min.time())
    if tzid and start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo(tzid))
    event.add("DTSTART", start)
    event.add("DTEND", start + duration)


def recurrence_to_rrule(recurrence: dict[str, Any]) -> dict[str, Any]:
    """Validate a recurrence dict and render it as RRULE parts."""
    if not isinstance(recurrence, dict):
        raise ICalError("recurrence must be an object, for example {\"frequency\": \"weekly\"}.")

    frequency = str(recurrence.get("frequency", "")).strip().lower()
    if frequency not in _FREQUENCIES:
        raise ICalError(
            f"recurrence.frequency must be one of {', '.join(_FREQUENCIES)}; "
            f"got {recurrence.get('frequency')!r}."
        )

    parts: dict[str, Any] = {"FREQ": frequency.upper()}

    interval = recurrence.get("interval")
    if interval not in (None, "", 1):
        try:
            parts["INTERVAL"] = int(interval)
        except (TypeError, ValueError):
            raise ICalError("recurrence.interval must be a whole number.") from None
        if parts["INTERVAL"] < 1:
            raise ICalError("recurrence.interval must be at least 1.")

    by_day = recurrence.get("byDay")
    if by_day:
        if isinstance(by_day, str):
            by_day = [by_day]
        days = [str(day).strip().lower() for day in by_day]
        unknown = [day for day in days if day not in _WEEKDAYS]
        if unknown:
            raise ICalError(
                f"recurrence.byDay entries must be among {', '.join(_WEEKDAYS)}; "
                f"got {', '.join(repr(day) for day in unknown)}."
            )
        parts["BYDAY"] = [day.upper() for day in days]

    count = recurrence.get("count")
    until = recurrence.get("until")
    if count and until:
        raise ICalError("recurrence takes count or until, not both.")

    if count:
        try:
            parts["COUNT"] = int(count)
        except (TypeError, ValueError):
            raise ICalError("recurrence.count must be a whole number.") from None

    if until:
        try:
            moment = datetime.fromisoformat(str(until))
        except ValueError:
            raise ICalError(
                "recurrence.until must be an ISO 8601 date or date-time."
            ) from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        parts["UNTIL"] = moment.astimezone(timezone.utc)

    return parts


def touch(event: IEvent) -> None:
    """Mark an event as modified, the way every other CalDAV client does."""
    for name in ("DTSTAMP", "LAST-MODIFIED"):
        if name in event:
            del event[name]
    event.add("DTSTAMP", datetime.now(timezone.utc))
    event.add("LAST-MODIFIED", datetime.now(timezone.utc))

    sequence = event.get("SEQUENCE")
    try:
        current = int(sequence) if sequence is not None else 0
    except (TypeError, ValueError):
        current = 0
    if "SEQUENCE" in event:
        del event["SEQUENCE"]
    event.add("SEQUENCE", current + 1)


def set_field(event: IEvent, name: str, value: str) -> None:
    """Replace a simple text property, removing it entirely when cleared."""
    if name in event:
        del event[name]
    if value:
        event.add(name, vText(value))


def make_address(email: str, name: str = "") -> vCalAddress:
    address = vCalAddress(f"mailto:{email}")
    if name:
        address.params["CN"] = vText(name)
    address.params["EMAIL"] = vText(email)
    return address


# ----------------------------------------------------------------------
# Scheduling
#
# iCloud advertises ``calendar-auto-schedule``, so it sends the iMIP mail
# itself (RFC 6638): PUT an event carrying an ORGANIZER that matches one of the
# account's own calendar-user-addresses plus ATTENDEE lines, and invitations go
# out. Change your own ATTENDEE's PARTSTAT and a REPLY goes to the organizer.
# Nothing here speaks SMTP, and nothing here can un-send anything.
# ----------------------------------------------------------------------


def attendees_of(event: IEvent) -> list[vCalAddress]:
    """Every ATTENDEE on an event, always as a list."""
    found = event.get("ATTENDEE")
    if found is None:
        return []
    return list(found) if isinstance(found, list) else [found]


def organizer_email(event: IEvent) -> str:
    return _address_email(event.get("ORGANIZER"))


def set_organizer(event: IEvent, email: str, name: str = "") -> None:
    if "ORGANIZER" in event:
        del event["ORGANIZER"]
    event.add("ORGANIZER", make_address(email, name))


def _write_attendees(event: IEvent, attendees: list[vCalAddress]) -> None:
    """Replace the ATTENDEE list wholesale.

    icalendar appends on ``add``, so removing one means rebuilding the set.
    """
    if "ATTENDEE" in event:
        del event["ATTENDEE"]
    for attendee in attendees:
        event.add("ATTENDEE", attendee)


def add_attendee(
    event: IEvent,
    email: str,
    name: str = "",
    *,
    partstat: str = "NEEDS-ACTION",
    rsvp: bool = True,
) -> bool:
    """Invite one person. Returns False if they were already invited.

    Matching is on the lowercased address: an invitation sent twice to the same
    person with different capitalization is a duplicate, not a second guest.
    """
    existing = attendees_of(event)
    if any(_address_email(a).lower() == email.lower() for a in existing):
        return False

    attendee = make_address(email, name)
    attendee.params["CUTYPE"] = vText("INDIVIDUAL")
    attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
    attendee.params["PARTSTAT"] = vText(partstat)
    if rsvp:
        attendee.params["RSVP"] = vText("TRUE")
    _write_attendees(event, [*existing, attendee])
    return True


def remove_attendees(
    event: IEvent, targets: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Uninvite people by email or display name.

    Returns ``(removed, unmatched, ambiguous)``. The organizer is never removed
    -- uninviting them is a different operation (cancel the event) and doing it
    by accident would orphan the series for everyone else.
    """
    attendees = attendees_of(event)
    organizer = organizer_email(event).lower()
    removed: list[str] = []
    unmatched: list[str] = []
    ambiguous: list[str] = []
    drop: set[int] = set()

    for target in targets:
        needle = target.strip().lower()
        if not needle:
            continue
        if "@" in needle:
            hits = [
                i
                for i, a in enumerate(attendees)
                if _address_email(a).lower() == needle
            ]
        else:
            hits = [
                i
                for i, a in enumerate(attendees)
                if str(a.params.get("CN", "")).strip().lower() == needle
            ]

        hits = [i for i in hits if _address_email(attendees[i]).lower() != organizer]

        if not hits:
            unmatched.append(target)
        elif len(hits) > 1:
            ambiguous.append(target)
        else:
            drop.add(hits[0])
            removed.append(_address_email(attendees[hits[0]]) or target)

    if drop:
        _write_attendees(
            event, [a for i, a in enumerate(attendees) if i not in drop]
        )
    return removed, unmatched, ambiguous


def set_partstat(event: IEvent, addresses: tuple[str, ...], status: str) -> str:
    """Set the account's own PARTSTAT on an event. Returns the address matched.

    ``addresses`` are the account's calendar-user-addresses; an account often
    has several and the invitation only ever names one of them. Raises if none
    of them is on the guest list, because silently inviting yourself in order to
    answer would be a strange thing to do on someone else's event.
    """
    attendees = attendees_of(event)
    if not attendees:
        raise ICalError(
            "This event has no participants, so there is nothing to respond to."
        )

    owned = {a.lower().removeprefix("mailto:") for a in addresses}
    for attendee in attendees:
        email = _address_email(attendee).lower()
        if email in owned:
            attendee.params["PARTSTAT"] = vText(status.upper())
            # Answering settles the question, so the server should stop asking.
            attendee.params["RSVP"] = vText("FALSE")
            _write_attendees(event, attendees)
            return email

    invited = ", ".join(_address_email(a) for a in attendees if _address_email(a))
    raise ICalError(
        "You are not on this event's guest list, so there is no invitation to "
        f"respond to. Invited: {invited or '(nobody with an address)'}."
    )


__all__ = [
    "DateError",
    "ICalError",
    "PRODID",
    "build_event",
    "build_resource",
    "add_attendee",
    "attendees_of",
    "event_to_dict",
    "organizer_email",
    "recurrence_id_value",
    "recurrence_key",
    "remove_attendees",
    "set_organizer",
    "set_partstat",
    "make_address",
    "new_uid",
    "parse_duration",
    "parse_resource",
    "recurrence_to_rrule",
    "set_field",
    "touch",
    "_set_timing",
]
