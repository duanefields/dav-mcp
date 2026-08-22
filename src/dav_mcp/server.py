"""The MCP server: tool definitions, health endpoint, and transport selection.

The tool surface mirrors the Fastmail calendar tools on purpose. ``compose_event``
is deliberately absent -- it stages an event into a confirmation widget that only
exists inside claude.ai, and has no meaning in a generic MCP client.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import platform
import time
from datetime import datetime, timedelta
from datetime import time as clock_time
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from pydantic import Field
from fastmcp.tools.tool import ToolResult
from starlette.responses import JSONResponse

from . import availability, caldav, carddav, ical, ids, vcard
from .caldav import CalDavClient, CalDavError
from .dates import (
    DateError,
    format_duration,
    has_explicit_offset,
    local_zone,
    parse_duration,
    parse_when,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("DAV")

MAX_LIMIT = 50
DEFAULT_LIMIT = 10

# Mirrors the Fastmail defaults: far enough back to catch "when did I last see
# them", far enough forward to catch anything already scheduled.
DEFAULT_AFTER = "3 months ago"
DEFAULT_BEFORE = "12 months from now"

_client: CalDavClient | None = None
_contacts: carddav.CardDavClient | None = None

# Enough to tell "never wrote" from "wrote and it failed" on a headless host,
# which is the same question things-mcp answers with last_write_dispatch.
_last_write: dict[str, Any] = {"at": None, "ok": None, "error": None, "action": None}


def client() -> CalDavClient:
    """The process-wide CalDAV client, built on first use.

    Built lazily rather than at import so that a missing credential surfaces as a
    tool error the caller can read, instead of preventing the server from
    starting at all.
    """
    global _client
    if _client is None:
        _client = caldav.client_from_env()
    return _client


def contacts() -> carddav.CardDavClient:
    """The process-wide CardDAV client, built on first use.

    Deliberately the same credentials as the calendar client -- one Apple ID
    serves both -- but a separate connection, because they address different
    hosts.
    """
    global _contacts
    if _contacts is None:
        _contacts = carddav.client_from_env()
    return _contacts


def _record_write(action: str, ok: bool, exc: Exception | None = None) -> None:
    """Note the outcome of the last write, for ``/health`` to report.

    Takes the exception rather than a message, and keeps only its class name.
    ``/health`` is unauthenticated, and a ``DavError``'s message carries the
    request URL -- which names the account's principal, the calendar and the
    resource -- plus up to 400 characters of the server's response body.
    ``healthcheck.sh`` then forwards it off the host to a ping service.

    The class name is what an operator actually needs: ``AuthError`` means the
    app-specific password was revoked, ``Throttled`` means wait, ``Conflict``
    means something else edited the resource. None of them leak anything.
    """
    _last_write.update(
        {
            "at": time.time(),
            "ok": ok,
            "error": type(exc).__name__ if exc is not None else None,
            "action": action,
        }
    )


# ----------------------------------------------------------------------
# Response shaping
# ----------------------------------------------------------------------


def _error_result(message: str) -> ToolResult:
    """A failure the model should read and act on, in both channels."""
    return ToolResult(content=message, structured_content={"error": message})


def _result(text: str, structured: dict[str, Any]) -> ToolResult:
    return ToolResult(content=text, structured_content=_json_safe(structured))


def _json_safe(value: Any) -> Any:
    """Coerce a payload to JSON primitives; dates become ISO strings."""
    return json.loads(json.dumps(value, default=str))


def _events_result(events: list[dict[str, Any]], total: int, limit: int, offset: int) -> ToolResult:
    page = events
    if not page:
        text = "No events found."
    else:
        header = (
            f"Showing {offset + 1}-{offset + len(page)} of {total} events\n\n"
            if total > len(page) or offset
            else ""
        )
        text = header + "\n\n---\n\n".join(format_event(event) for event in page)

    return _result(
        text,
        {
            "items": page,
            "count": len(page),
            "total": total,
            "offset": offset,
            "limit": limit,
        },
    )


def format_event(event: dict[str, Any]) -> str:
    """Render one event as the human-readable text channel."""
    lines = [f"Title: {event.get('title') or '(no title)'}"]

    start = event.get("start", "")
    if event.get("isAllDay"):
        day = start.split("T")[0]
        duration = event.get("duration", "P1D")
        lines.append(
            f"When: {day} (all day)"
            if duration == "P1D"
            else f"When: {day} (all day, {duration})"
        )
    else:
        zone = event.get("timeZone") or ""
        lines.append(f"When: {start}{f' {zone}' if zone else ''} for {event.get('duration', '')}")

    for location in event.get("locations") or []:
        lines.append(f"Location: {location}")
    for location in event.get("virtualLocations") or []:
        lines.append(f"Virtual: {location}")

    recurrence = event.get("recurrence")
    if recurrence:
        lines.append(f"Repeats: {_describe_recurrence(recurrence)}")
    elif event.get("recurrenceId"):
        lines.append("Repeats: one occurrence of a recurring event")

    participants = event.get("participants") or []
    if participants:
        rendered = ", ".join(
            f"{person.get('name') or person.get('email')}"
            f"{f' ({person['status']})' if person.get('status') else ''}"
            for person in participants
        )
        lines.append(f"Participants: {rendered}")

    description = (event.get("description") or "").strip()
    if description:
        collapsed = " ".join(description.split())
        lines.append(
            f"Notes: {collapsed[:300]}{'...' if len(collapsed) > 300 else ''}"
        )

    lines.append(f"ID: {event.get('id')}")
    return "\n".join(lines)


def _describe_recurrence(recurrence: dict[str, Any]) -> str:
    frequency = recurrence.get("frequency", "")
    interval = recurrence.get("interval", 1)
    text = f"every {interval} {frequency[:-2] if frequency.endswith('ly') else frequency}s" \
        if interval and interval != 1 else frequency
    if recurrence.get("byDay"):
        text += " on " + ", ".join(str(day).upper() for day in recurrence["byDay"])
    if recurrence.get("count"):
        text += f", {recurrence['count']} times"
    if recurrence.get("until"):
        text += f", until {recurrence['until']}"
    return text


def _matches(event: dict[str, Any], needle: str) -> bool:
    """Case-insensitive substring match across the fields a user would mean.

    Done here rather than in the CalDAV query because iCloud ignores a text
    filter whenever a time range is present; see ``caldav.CalDavClient.query``.
    """
    haystack = [
        event.get("title", ""),
        event.get("description", ""),
        *(event.get("locations") or []),
        *(event.get("virtualLocations") or []),
    ]
    for person in event.get("participants") or []:
        haystack.append(person.get("name", ""))
        haystack.append(person.get("email", ""))
    return needle in " \n".join(haystack).lower()


RSVP_STATUSES = {"accepted": "ACCEPTED", "tentative": "TENTATIVE", "declined": "DECLINED"}


def _clean_participants(participants: list | None) -> tuple[list[dict], str | None]:
    """Validate the participants argument. Returns ``(people, error)``.

    Rejected wholesale rather than partially: half-inviting a list because one
    entry was malformed sends real mail to some people and not others, and
    there is no way to take it back.
    """
    if not participants:
        return [], None

    if not isinstance(participants, list):
        return [], 'participants must be a list, for example [{"name": "Jo", "email": "jo@example.com"}].'

    people: list[dict] = []
    for index, entry in enumerate(participants):
        if not isinstance(entry, dict):
            return [], (
                f"participants[{index}] must be an object with an email, "
                f'for example {{"name": "Jo", "email": "jo@example.com"}}; got {entry!r}.'
            )
        email = str(entry.get("email", "")).strip()
        if not email or "@" not in email:
            return [], (
                f"participants[{index}] needs a valid email address; got {entry.get('email')!r}."
            )
        people.append({"email": email, "name": str(entry.get("name", "")).strip()})

    seen: set[str] = set()
    unique = []
    for person in people:
        key = person["email"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(person)
    return unique, None


async def _delivery_report(target, resource_name: str) -> dict[str, str]:
    """Read back what iCloud managed to deliver, per attendee.

    iCloud stamps ``SCHEDULE-STATUS`` on each ATTENDEE after it attempts
    delivery (RFC 6638). Without reading it back, a write that iCloud accepted
    and then failed to deliver is indistinguishable from a successful one, and
    reporting "invitations sent" for a message that bounced is worse than
    saying nothing -- the user stops chasing an invitation that never arrived.

    Costs one extra GET, and only on writes that touch participants.
    """
    try:
        resource = await client().get(target, resource_name)
        event = ical.parse_resource(resource.ics)[0]
    except (CalDavError, ical.ICalError, ValueError) as exc:
        logger.warning("Could not read back delivery status: %s", exc)
        return {}

    report: dict[str, str] = {}
    for attendee in ical.attendees_of(event):
        email = ical._address_email(attendee)
        status = str(attendee.params.get("SCHEDULE-STATUS", "")).strip()
        if email and status:
            report[email.lower()] = status
    return report


def _describe_delivery(emails: list[str], report: dict[str, str]) -> tuple[str, list[str]]:
    """Turn a delivery report into a sentence. Returns ``(text, failed)``.

    Anything in the 1.x family means iCloud sent or delivered it. 3.x and 5.x
    are failures -- an unroutable address, or a recipient whose mail server
    refused the message.
    """
    delivered, failed, unknown = [], [], []
    for email in emails:
        status = report.get(email.lower(), "")
        code = status.split(";")[0].strip()
        if not code:
            unknown.append(email)
        elif code.startswith("1."):
            delivered.append(email)
        else:
            failed.append(f"{email} ({code})")

    parts = []
    if delivered:
        parts.append("iCloud delivered invitations to " + ", ".join(delivered) + ".")
    if failed:
        parts.append(
            "iCloud COULD NOT deliver to " + ", ".join(failed) + " -- they have "
            "not been told about this event. Check the address."
        )
    if unknown:
        parts.append(
            "Delivery to " + ", ".join(unknown) + " is still pending; iCloud "
            "reported no status yet."
        )
    return " ".join(parts), failed


async def _resolve_organizer(requested: str | None) -> tuple[str, str | None]:
    """Pick the address to organize from. Returns ``(email, error)``.

    It must be one of the account's own calendar-user-addresses. iCloud will
    not schedule on behalf of an address it does not recognize as ours -- it
    accepts the PUT and quietly sends nothing, which looks exactly like success.
    """
    identities = await client().identities()
    usable = [address.removeprefix("mailto:") for address in identities]
    if not usable:
        return "", (
            "This account publishes no scheduling addresses, so it cannot send "
            "invitations."
        )

    if not requested:
        return usable[0], None

    wanted = requested.strip().lower().removeprefix("mailto:")
    for address in usable:
        if address.lower() == wanted:
            return address, None

    return "", (
        f"{requested!r} is not one of this account's addresses, so iCloud will "
        "not send invitations from it. Available: " + ", ".join(usable) + "."
    )


def _validate_limit(limit: int | None) -> str | None:
    if limit is None:
        return None
    if not isinstance(limit, int) or isinstance(limit, bool):
        return f"limit must be a whole number, got {limit!r}."
    if limit < 1:
        return "limit must be at least 1."
    if limit > MAX_LIMIT:
        return f"limit cannot exceed {MAX_LIMIT}."
    return None


# ----------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------


@mcp.tool
async def list_calendars() -> ToolResult:
    """List the user's calendars.

    Returns calendar IDs, names, and colors. Use this to find which calendar to
    add events to. Only calendars that hold events are listed; reminder lists
    are not part of this server's surface.
    """
    try:
        calendars = await client().calendars()
    except (CalDavError, ValueError) as exc:
        return _error_result(str(exc))

    if not calendars:
        return _error_result("The account has no event calendars.")

    default = next((cal for cal in calendars if not cal.read_only), None)
    items = [
        {
            "id": cal.id,
            "name": cal.name,
            "color": cal.color,
            "isDefault": cal is default,
            "readOnly": cal.read_only,
        }
        for cal in calendars
    ]

    text = "\n".join(
        f"{item['name']} ({item['id']})"
        f"{' [default]' if item['isDefault'] else ''}"
        f"{' [read-only]' if item['readOnly'] else ''}"
        for item in items
    )
    return _result(text, {"items": items, "count": len(items)})


@mcp.tool
async def search_events(
    query: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int | None = None,
    calendarId: str | None = None,
) -> ToolResult:
    """List or search calendar events.

    To list a period (e.g. "what's on tomorrow"), pass only 'after'/'before' and
    omit 'query'. Recurring events are expanded into individual occurrences, so
    each occurrence has its own ID and can be updated or deleted independently.

    Returns full details including title, start, duration, time zone,
    description, locations, participants with RSVP status, and recurrence info.

    Args:
        query: Optional free-text filter against titles, descriptions, locations
            and participants. Omit to return everything in the range; never pass
            a placeholder like "a" or "*".
        after: Only return events starting at or after this time. Accepts ISO
            8601 ("2026-03-15", "2026-03-15T14:00:00") or a relative expression
            ("now", "today", "tomorrow", "yesterday", "3 days ago",
            "2 weeks from now"). Defaults to 3 months ago.
        before: Only return events starting before this time. Same formats as
            'after'. Defaults to 12 months from now.
        limit: Maximum number of results (default 10, max 50).
        calendarId: Restrict the search to one calendar. Defaults to all of them.
    """
    error = _validate_limit(limit)
    if error:
        return _error_result(error)
    effective_limit = DEFAULT_LIMIT if limit is None else limit

    zone = local_zone()
    now = datetime.now(zone)
    try:
        start = parse_when(after, default=now - timedelta(days=90), tz=zone)
        end = parse_when(before, default=now + timedelta(days=365), tz=zone)
    except DateError as exc:
        return _error_result(str(exc))

    if end <= start:
        return _error_result(
            f"The range is empty: 'before' ({end.isoformat()}) is not after "
            f"'after' ({start.isoformat()})."
        )

    try:
        calendars = await client().calendars()
        if calendarId:
            calendars = [cal for cal in calendars if cal.id == calendarId]
            if not calendars:
                return _error_result(
                    f"No calendar with id {calendarId!r}. Call list_calendars to "
                    "see the available calendar ids."
                )

        events: list[dict[str, Any]] = []
        for cal in calendars:
            for resource in await client().query(cal, start=start, end=end):
                try:
                    for event in ical.parse_resource(resource.ics):
                        events.append(
                            ical.event_to_dict(
                                event,
                                calendar_id=resource.calendar_id,
                                resource_name=resource.name,
                                calendar_color=cal.color,
                            )
                        )
                except ical.ICalError as exc:
                    # One unreadable event should not take out the whole search.
                    logger.warning("Skipping %s: %s", resource.url, exc)
    except (CalDavError, ValueError) as exc:
        return _error_result(str(exc))

    if query and query.strip():
        needle = query.strip().lower()
        events = [event for event in events if _matches(event, needle)]

    events.sort(key=lambda event: (event.get("start", ""), event.get("title", "")))
    total = len(events)
    return _events_result(events[:effective_limit], total, effective_limit, 0)


def _parse_clock(value: str | None, default: clock_time, name: str) -> clock_time:
    """Parse a "HH:MM" working-hours bound."""
    if value is None or not str(value).strip():
        return default
    text = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%H"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise DateError(f'{name} must look like "09:00"; got {value!r}.')


def _busy_intervals(
    events: list[dict[str, Any]], identities: set[str], zone
) -> tuple[list[tuple[datetime, datetime]], list[dict[str, Any]]]:
    """Turn events into busy intervals. Returns ``(busy, all_day_events)``.

    Three kinds of event are deliberately not treated as busy:

    * Anything the event itself marks free -- ``TRANSP:TRANSPARENT`` or
      ``STATUS:CANCELLED`` (see ``ical._blocks_time``).
    * Anything the user declined. Apple leaves declined invitations on the
      calendar, and counting a meeting you refused would block the week with
      things you are not attending.
    * All-day events. Whether one occupies the day is genuinely ambiguous --
      "First Day of School" does not, a multi-day trip does -- so they are
      excluded from the arithmetic and handed back separately for the caller
      to mention rather than silently guessed at.
    """
    busy: list[tuple[datetime, datetime]] = []
    all_day: list[dict[str, Any]] = []

    for event in events:
        if event.get("isAllDay"):
            all_day.append(event)
            continue
        if not event.get("blocksTime", True):
            continue

        mine = [
            person
            for person in event.get("participants") or []
            if person.get("email", "").lower() in identities
        ]
        if mine and all(person.get("status") == "declined" for person in mine):
            continue

        try:
            naive = datetime.fromisoformat(event["start"])
            tzid = event.get("timeZone") or ""
            start = naive.replace(tzinfo=ZoneInfo(tzid) if tzid else zone)
            end = start + parse_duration(event["duration"])
        except (ValueError, KeyError, DateError):
            logger.warning("Skipping unreadable event in availability: %s", event.get("id"))
            continue

        busy.append((start, end))

    return busy, all_day


@mcp.tool
async def find_free_time(
    duration: str,
    after: str | None = None,
    before: str | None = None,
    calendarIds: list | None = None,
    dayStart: str | None = None,
    dayEnd: str | None = None,
    includeWeekends: bool | None = None,
    limit: int | None = None,
) -> ToolResult:
    """Find open slots on the user's calendars that are long enough for something.

    Use this to answer "when am I free for X" or to pick a time before calling
    create_event. To ask what is already scheduled, use search_events instead.

    Openings are reported at their full length, not trimmed to 'duration', so a
    two-hour gap is reported as two hours even when asked for one.

    Not counted as busy: events marked free (TRANSP:TRANSPARENT), cancelled
    events, and invitations the user declined. All-day events are also not
    counted -- a birthday does not occupy the day -- but any that overlap the
    search are listed separately so they can be taken into account.

    Args:
        duration: How long the slot needs to be, ISO 8601, e.g. "PT1H" or
            "PT30M".
        after: Earliest time to consider. ISO 8601 or a relative expression
            ("today", "tomorrow", "2 weeks from now"). Defaults to now.
        before: Latest time to consider. Same formats. Defaults to two weeks
            from now.
        calendarIds: Restrict to these calendars. Defaults to all of them.
        dayStart: Earliest hour of day to offer, "HH:MM". Defaults to "09:00".
            Pass "00:00" together with dayEnd "23:59" to search around the clock.
        dayEnd: Latest hour of day to offer, "HH:MM". Defaults to "17:00".
        includeWeekends: Offer Saturday and Sunday too. Defaults to false.
        limit: Maximum number of openings to return (default 10, max 50).
    """
    error = _validate_limit(limit)
    if error:
        return _error_result(error)
    effective_limit = DEFAULT_LIMIT if limit is None else limit

    zone = local_zone()
    now = datetime.now(zone)

    try:
        needed = parse_duration(duration)
        if needed <= timedelta(0):
            return _error_result("duration must be positive, for example \"PT1H\".")
        start = parse_when(after, default=now, tz=zone)
        end = parse_when(before, default=now + timedelta(days=14), tz=zone)
        opens = _parse_clock(dayStart, clock_time(9, 0), "dayStart")
        closes = _parse_clock(dayEnd, clock_time(17, 0), "dayEnd")
    except DateError as exc:
        return _error_result(str(exc))

    if end <= start:
        return _error_result(
            f"The range is empty: 'before' ({end.isoformat()}) is not after "
            f"'after' ({start.isoformat()})."
        )
    if closes <= opens:
        return _error_result(
            f"dayEnd ({closes.strftime('%H:%M')}) must be later than dayStart "
            f"({opens.strftime('%H:%M')})."
        )

    weekdays = set(range(7)) if includeWeekends else set(range(5))

    try:
        calendars = await client().calendars()
        if calendarIds:
            wanted = {str(cid) for cid in calendarIds}
            calendars = [cal for cal in calendars if cal.id in wanted]
            if not calendars:
                return _error_result(
                    "None of those calendar ids exist. Call list_calendars to "
                    "see the available ids."
                )

        events: list[dict[str, Any]] = []
        for cal in calendars:
            for resource in await client().query(cal, start=start, end=end):
                try:
                    for parsed in ical.parse_resource(resource.ics):
                        events.append(
                            ical.event_to_dict(
                                parsed,
                                calendar_id=resource.calendar_id,
                                resource_name=resource.name,
                                calendar_color=cal.color,
                            )
                        )
                except ical.ICalError as exc:
                    logger.warning("Skipping %s: %s", resource.url, exc)
        identities = {a.removeprefix("mailto:").lower() for a in await client().identities()}
    except (CalDavError, ValueError) as exc:
        return _error_result(str(exc))

    busy, all_day = _busy_intervals(events, identities, zone)

    try:
        windows = availability.working_windows(
            start, end, tz=zone, day_start=opens, day_end=closes, weekdays=weekdays
        )
        openings = availability.slots(windows, busy, needed, limit=effective_limit)
    except ValueError as exc:
        return _error_result(str(exc))

    return _free_time_result(
        openings, needed, all_day, zone, effective_limit, bool(includeWeekends)
    )


def _free_time_result(openings, needed, all_day, zone, limit, weekends) -> ToolResult:
    items = [
        {
            "start": open_start.astimezone(zone).strftime("%Y-%m-%dT%H:%M:%S"),
            "end": open_end.astimezone(zone).strftime("%Y-%m-%dT%H:%M:%S"),
            "duration": format_duration(open_end - open_start),
            "timeZone": str(zone),
        }
        for open_start, open_end in openings
    ]

    if not items:
        text = (
            f"No opening of at least {format_duration(needed)} in that range. "
            "Widen the range, shorten the duration, or relax dayStart/dayEnd"
            + ("" if weekends else " (weekends are excluded unless you ask for them)")
            + "."
        )
    else:
        lines = []
        current_day = None
        for open_start, open_end in openings:
            local_start = open_start.astimezone(zone)
            local_end = open_end.astimezone(zone)
            day = local_start.strftime("%a %Y-%m-%d")
            if day != current_day:
                lines.append(day)
                current_day = day
            lines.append(
                f"  {local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')}"
                f"  ({format_duration(local_end - local_start)} free)"
            )
        text = (
            f"{len(items)} opening(s) of at least {format_duration(needed)} "
            f"({zone}):\n\n" + "\n".join(lines)
        )

    if all_day:
        titles = sorted({event.get("title") or "(untitled)" for event in all_day})
        text += (
            "\n\nAll-day events in this range are not treated as busy: "
            + ", ".join(titles)
            + "."
        )

    return _result(
        text,
        {
            "items": items,
            "count": len(items),
            "limit": limit,
            "duration": format_duration(needed),
            "timeZone": str(zone),
            "allDayEvents": [
                {"title": e.get("title"), "start": e.get("start"), "id": e.get("id")}
                for e in all_day
            ],
        },
    )


@mcp.tool
async def create_event(
    title: str,
    start: str,
    calendarId: str | None = None,
    description: str | None = None,
    duration: str | None = None,
    isAllDay: bool | None = None,
    location: str | None = None,
    timeZone: str | None = None,
    color: str | None = None,
    recurrence: dict | None = None,
    participants: list | None = None,
    organizer: Annotated[
        str | None,
        Field(
            alias="from",
            description=(
                "Email address to organize the event from when inviting "
                "participants. Must match one of the account's own addresses. "
                "If omitted, the account's primary address is used."
            ),
        ),
    ] = None,
) -> ToolResult:
    """Create a calendar event. Returns the new event's id.

    Use update_event to change an existing event.

    Args:
        title: Event title.
        start: Start date-time in ISO 8601 format (e.g. "2026-03-15T14:00:00").
        calendarId: Calendar to add the event to. Use list_calendars to find
            calendar IDs. Defaults to the user's first writable calendar.
        description: Event description or notes.
        duration: Duration in ISO 8601 format, e.g. "PT1H" (1 hour) or "PT30M"
            (30 minutes). For an all-day event set isAllDay and give a whole-day
            duration like "P1D" or "P2D". Defaults to PT1H, or P1D when isAllDay.
        isAllDay: Set true for a true all-day (date-only) event such as a
            birthday, holiday, or multi-day trip. All-day events float: they have
            no time of day and no time zone, and never shift when viewed in
            another zone. Give 'start' as a date ("2026-07-03"; any time part is
            ignored) and 'duration' in whole days. (Note: a midnight start with
            duration P1D is NOT an all-day event -- it is a 24-hour timed block.
            Use this flag instead.)
        location: Location name or address.
        timeZone: IANA time zone (e.g. "America/New_York"). Defaults to the
            server's zone. Ignored for all-day events, which have no time zone.
        color: Display color, as a six-digit hex value (e.g. "#ff0000").
        recurrence: Make this a recurring event. Properties: frequency
            (required, one of "daily", "weekly", "monthly", "yearly"), interval
            (repeat every N, default 1), byDay (array of day codes for weekly:
            ["mo","tu","we","th","fr","sa","su"]), count (stop after N
            occurrences), until (stop after this date-time). Examples:
            {"frequency": "weekly"}; {"frequency": "weekly", "interval": 2,
            "byDay": ["mo","we","fr"]}.
        participants: Invitees, e.g. [{"name": "Jo", "email": "jo@example.com"}].
            The account is added automatically as organizer; do not include it
            here. THIS SENDS REAL INVITATION EMAILS immediately, and they cannot
            be recalled -- omit this argument unless the user asked for guests.
    """
    if not title or not title.strip():
        return _error_result("title is required.")

    people, error = _clean_participants(participants)
    if error:
        return _error_result(error)

    all_day = bool(isAllDay)
    zone = local_zone()

    try:
        moment = parse_when(start, default=None, tz=zone)
        if moment is None:
            return _error_result("start is required.")
        length = parse_duration(
            duration if duration else ("P1D" if all_day else "PT1H")
        )
    except DateError as exc:
        return _error_result(str(exc))

    tzid = "" if all_day else (timeZone or str(zone))
    if tzid:
        try:
            from zoneinfo import ZoneInfo

            target_zone = ZoneInfo(tzid)
        except Exception:
            return _error_result(
                f"{tzid!r} is not a known IANA time zone. Use a name like "
                '"America/New_York" or "Europe/London".'
            )
        # A start that named its own offset identifies an instant, so convert it.
        # A bare wall-clock start means that time *in* timeZone, so relabel it.
        moment = (
            moment.astimezone(target_zone)
            if has_explicit_offset(start)
            else moment.replace(tzinfo=target_zone)
        )

    try:
        target = (
            await client().calendar(calendarId)
            if calendarId
            else await client().default_calendar()
        )
    except (CalDavError, ValueError) as exc:
        return _error_result(str(exc))

    if target.read_only:
        return _error_result(f"The calendar {target.name!r} is read-only.")

    organizer_email = ""
    if people:
        organizer_email, error = await _resolve_organizer(organizer)
        if error:
            return _error_result(error)
    elif organizer:
        return _error_result(
            "'from' only applies when inviting participants. Pass participants "
            "as well, or omit 'from'."
        )

    try:
        event = ical.build_event(
            uid=ical.new_uid(),
            title=title.strip(),
            start=moment,
            duration=length,
            all_day=all_day,
            tzid=tzid,
            description=(description or "").strip(),
            location=(location or "").strip(),
            color=(color or "").strip(),
            recurrence=recurrence,
        )
        if people:
            # The organizer is also an attendee, and has by definition accepted;
            # without this they do not appear on their own guest list.
            ical.set_organizer(event, organizer_email)
            ical.add_attendee(
                event, organizer_email, partstat="ACCEPTED", rsvp=False
            )
            for person in people:
                ical.add_attendee(event, person["email"], person["name"])
        uid = str(event["UID"])
        body = ical.build_resource([event], {tzid} if tzid else set())
    except (ical.ICalError, ValueError) as exc:
        return _error_result(str(exc))

    name = f"{uid}.ics"
    try:
        await client().put(target, name, body, create=True)
    except CalDavError as exc:
        _record_write("create_event", False, exc)
        return _error_result(f"Could not create the event: {exc}")

    _record_write("create_event", True)
    event_id = ids.encode(target.id, name)
    payload = ical.event_to_dict(
        event,
        calendar_id=target.id,
        resource_name=name,
        calendar_color=target.color,
    )
    headline = f"Created event in {target.name}."
    failed: list[str] = []
    if people:
        emails = [person["email"] for person in people]
        sentence, failed = _describe_delivery(
            emails, await _delivery_report(target, name)
        )
        headline += f" Organized by {organizer_email}. {sentence}"

    return _result(
        f"{headline}\n\n{format_event(payload)}",
        {
            "id": event_id,
            "event": payload,
            "invited": [p["email"] for p in people],
            "undelivered": failed,
        },
    )


@mcp.tool
async def update_event(
    id: str,
    title: str | None = None,
    start: str | None = None,
    duration: str | None = None,
    description: str | None = None,
    location: str | None = None,
    timeZone: str | None = None,
    isAllDay: bool | None = None,
    color: str | None = None,
    recurrence: dict | None = None,
    addParticipants: list | None = None,
    removeParticipants: list | None = None,
    organizer: Annotated[
        str | None,
        Field(
            alias="from",
            description=(
                "Email address to organize from when adding the first "
                "participants. Must match one of the account's own addresses. "
                "Has no effect when the event already has an organizer."
            ),
        ),
    ] = None,
) -> ToolResult:
    """Update an existing calendar event.

    Use create_event for new events. Only specified fields are changed. For
    recurring events, pass an occurrence ID (from search_events) to modify just
    that single occurrence, or the master ID to change the whole series.

    Args:
        id: ID of the event to update, as returned by search_events.
        title: Event title.
        start: Start date-time in ISO 8601 format (e.g. "2026-03-15T14:00:00").
        duration: Duration in ISO 8601 format, e.g. "PT1H" or "P1D".
        description: Event description or notes. Pass an empty string to clear.
        location: Location name or address. Pass an empty string to clear.
        timeZone: IANA time zone. Ignored for all-day events.
        isAllDay: Set true to convert this into a true all-day (date-only,
            floating) event, or false to convert one back into a timed event.
        color: Six-digit hex color. Pass an empty string to clear it and inherit
            from the calendar.
        recurrence: Recurrence rules, same shape as create_event's recurrence.
            Only meaningful on a series master.
        addParticipants: Invitees to add, e.g. [{"name": "Jo", "email":
            "jo@example.com"}]. Duplicates against existing invitees are
            skipped. SENDS REAL INVITATION EMAILS.
        removeParticipants: Invitees to uninvite, by email or display name.
            Matching is case-insensitive; a name matching more than one invitee
            is an error, so pass the email to disambiguate. The organizer is
            never removed. SENDS REAL CANCELLATIONS.

    Note: any change to an event that already has invitees makes iCloud send an
    updated invitation to all of them. Mail goes out even for an edit as small
    as a typo fix, so say so rather than presenting the edit as silent.
    """
    try:
        calendar_id, resource_name, recurrence_id = ids.decode(id)
    except ids.BadEventId as exc:
        return _error_result(str(exc))

    joining, error = _clean_participants(addParticipants)
    if error:
        return _error_result(error)

    leaving = removeParticipants or []
    if leaving and not isinstance(leaving, list):
        return _error_result(
            'removeParticipants must be a list of emails or names, for example '
            '["jo@example.com"].'
        )

    try:
        target = await client().calendar(calendar_id)
        if target.read_only:
            return _error_result(f"The calendar {target.name!r} is read-only.")
        resource = await client().get(target, resource_name)
        events = ical.parse_resource(resource.ics)
    except (CalDavError, ical.ICalError, ValueError) as exc:
        return _error_result(str(exc))

    try:
        event, events = _select_occurrence(events, recurrence_id)
    except ical.ICalError as exc:
        return _error_result(str(exc))

    zone = local_zone()
    current = ical.event_to_dict(
        event,
        calendar_id=target.id,
        resource_name=resource_name,
        calendar_color=target.color,
    )

    all_day = current["isAllDay"] if isAllDay is None else bool(isAllDay)
    tzid = "" if all_day else (timeZone or current["timeZone"] or str(zone))

    try:
        if start is not None or duration is not None or isAllDay is not None:
            moment = (
                parse_when(start, default=None, tz=zone)
                if start is not None
                else parse_when(current["start"], default=None, tz=zone)
            )
            length = parse_duration(
                duration if duration is not None else current["duration"]
            )
            if tzid:
                moment = moment.replace(tzinfo=None)
            ical._set_timing(
                event, start=moment, duration=length, all_day=all_day, tzid=tzid
            )
    except DateError as exc:
        return _error_result(str(exc))

    if title is not None:
        if not title.strip():
            return _error_result("title cannot be empty.")
        ical.set_field(event, "SUMMARY", title.strip())
    if description is not None:
        ical.set_field(event, "DESCRIPTION", description.strip())
    if location is not None:
        ical.set_field(event, "LOCATION", location.strip())
    if color is not None:
        ical.set_field(event, "COLOR", color.strip())

    invited: list[str] = []
    uninvited: list[str] = []
    if joining or leaving:
        if recurrence_id:
            return _error_result(
                "Participants are a property of the whole series, not of one "
                "occurrence. Pass the master event's id rather than an "
                "occurrence id."
            )

        if joining and not ical.organizer_email(event):
            organizer_address, error = await _resolve_organizer(organizer)
            if error:
                return _error_result(error)
            ical.set_organizer(event, organizer_address)
            ical.add_attendee(
                event, organizer_address, partstat="ACCEPTED", rsvp=False
            )

        if leaving:
            removed, unmatched, ambiguous = ical.remove_attendees(
                event, [str(entry) for entry in leaving]
            )
            if ambiguous:
                return _error_result(
                    "More than one invitee matches "
                    + ", ".join(repr(name) for name in ambiguous)
                    + ". Pass the email address instead. Nothing was changed."
                )
            uninvited = removed
            if unmatched:
                logger.info("removeParticipants matched nothing: %s", unmatched)

        for person in joining:
            if ical.add_attendee(event, person["email"], person["name"]):
                invited.append(person["email"])

    if recurrence is not None:
        if recurrence_id:
            return _error_result(
                "Recurrence can only be changed on the whole series. Pass the "
                "master event's id rather than an occurrence id."
            )
        if "RRULE" in event:
            del event["RRULE"]
        if recurrence:
            try:
                event.add("RRULE", ical.recurrence_to_rrule(recurrence))
            except ical.ICalError as exc:
                return _error_result(str(exc))

    ical.touch(event)

    tzids = {tzid} if tzid else set()
    for other in events:
        other_tz = ical.event_to_dict(
            other, calendar_id=target.id, resource_name=resource_name
        )["timeZone"]
        if other_tz:
            tzids.add(other_tz)

    try:
        body = ical.build_resource(events, tzids)
        await client().put(target, resource_name, body, etag=resource.etag)
    except (CalDavError, ical.ICalError) as exc:
        _record_write("update_event", False, exc)
        return _error_result(f"Could not update the event: {exc}")

    _record_write("update_event", True)
    payload = ical.event_to_dict(
        event,
        calendar_id=target.id,
        resource_name=resource_name,
        calendar_color=target.color,
    )

    headline = "Updated event."
    failed: list[str] = []
    if invited:
        sentence, failed = _describe_delivery(
            invited, await _delivery_report(target, resource_name)
        )
        headline += f" {sentence}"
    if uninvited:
        headline += " Cancellations sent to " + ", ".join(uninvited) + "."
    if payload["participants"] and not (invited or uninvited):
        headline += (
            " This event has invitees, so iCloud has mailed them the update."
        )

    return _result(
        f"{headline}\n\n{format_event(payload)}",
        {
            "event": payload,
            "invited": invited,
            "uninvited": uninvited,
            "undelivered": failed,
        },
    )


def _select_occurrence(
    events: list[Any], recurrence_id: str
) -> tuple[Any, list[Any]]:
    """Pick the VEVENT an id refers to, creating an override if needed.

    Editing one occurrence of a series means adding a second VEVENT to the same
    resource carrying a RECURRENCE-ID. If one already exists we edit it in
    place; otherwise the master is cloned so the edit lands on the occurrence
    alone rather than the whole series.
    """
    master = events[0]
    if not recurrence_id:
        return master, events

    for event in events:
        prop = event.get("RECURRENCE-ID")
        if prop is not None and ical.recurrence_key(prop) == recurrence_id:
            return event, events

    if "RRULE" not in master:
        raise ical.ICalError(
            "That id names an occurrence of a recurring event, but the event on "
            "the server is not recurring. Re-run search_events to get a current id."
        )

    override = _clone_as_override(master, recurrence_id)
    return override, [*events, override]


def _clone_as_override(master: Any, recurrence_id: str) -> Any:
    from copy import deepcopy

    override = deepcopy(master)
    # An override describes a single instance, so the series-level properties
    # must not come along with the copy.
    for name in ("RRULE", "EXDATE", "RDATE", "RECURRENCE-ID"):
        if name in override:
            del override[name]

    override.add(
        "RECURRENCE-ID",
        ical.recurrence_id_value(recurrence_id, master.get("DTSTART").dt),
    )
    return override


@mcp.tool
async def rsvp_event(id: str, status: str) -> ToolResult:
    """Respond to a calendar event invitation.

    Sets your participation status and sends a reply to the organizer.

    For a recurring event, pass an occurrence id to respond for that occurrence
    alone, or the series id to respond to the whole series.

    Args:
        id: The event ID to RSVP to, as returned by search_events.
        status: Your response: accepted (going), tentative (maybe), or declined
            (not going).
    """
    wanted = (status or "").strip().lower()
    if wanted not in RSVP_STATUSES:
        return _error_result(
            f"status must be one of accepted, tentative, declined; got {status!r}."
        )

    try:
        calendar_id, resource_name, recurrence_id = ids.decode(id)
    except ids.BadEventId as exc:
        return _error_result(str(exc))

    try:
        target = await client().calendar(calendar_id)
        if target.read_only:
            return _error_result(
                f"The calendar {target.name!r} is read-only, so a reply cannot "
                "be recorded on it."
            )
        resource = await client().get(target, resource_name)
        events = ical.parse_resource(resource.ics)
        event, events = _select_occurrence(events, recurrence_id)
    except (CalDavError, ical.ICalError, ValueError) as exc:
        return _error_result(str(exc))

    organizer = ical.organizer_email(event)
    identities = await client().identities()
    owned = {a.lower().removeprefix("mailto:") for a in identities}
    if organizer and organizer.lower() in owned:
        return _error_result(
            "You are the organizer of this event, so there is no invitation to "
            "answer. Use update_event to change it, or delete_event to cancel it."
        )

    try:
        answered_as = ical.set_partstat(event, identities, RSVP_STATUSES[wanted])
    except ical.ICalError as exc:
        return _error_result(str(exc))

    ical.touch(event)

    try:
        tzids = {
            ical.event_to_dict(ev, calendar_id=target.id, resource_name=resource_name)[
                "timeZone"
            ]
            for ev in events
        }
        body = ical.build_resource(events, {tz for tz in tzids if tz})
        await client().put(target, resource_name, body, etag=resource.etag)
    except (CalDavError, ical.ICalError) as exc:
        _record_write("rsvp_event", False, exc)
        return _error_result(f"Could not record the reply: {exc}")

    _record_write("rsvp_event", True)
    payload = ical.event_to_dict(
        event,
        calendar_id=target.id,
        resource_name=resource_name,
        calendar_color=target.color,
    )
    told = f" iCloud has replied to {organizer}." if organizer else ""
    return _result(
        f"Responded {wanted} as {answered_as}.{told}\n\n{format_event(payload)}",
        {"status": wanted, "respondedAs": answered_as, "event": payload},
    )


@mcp.tool
async def delete_event(id: str) -> ToolResult:
    """Delete a calendar event.

    For recurring events, pass an occurrence id to cancel just that occurrence,
    or the master id to delete the entire series.

    Args:
        id: The event ID to delete, as returned by search_events.
    """
    try:
        calendar_id, resource_name, recurrence_id = ids.decode(id)
    except ids.BadEventId as exc:
        return _error_result(str(exc))

    try:
        target = await client().calendar(calendar_id)
        if target.read_only:
            return _error_result(f"The calendar {target.name!r} is read-only.")
    except (CalDavError, ValueError) as exc:
        return _error_result(str(exc))

    if not recurrence_id:
        try:
            await client().delete(target, resource_name)
        except caldav.NotFound:
            _record_write("delete_event", True)
            return _result(
                "That event no longer exists on the server; nothing to delete.",
                {"deleted": False, "reason": "not-found"},
            )
        except CalDavError as exc:
            _record_write("delete_event", False, exc)
            return _error_result(f"Could not delete the event: {exc}")

        _record_write("delete_event", True)
        return _result("Deleted the event.", {"deleted": True})

    # Cancelling one occurrence is an EXDATE on the master, not a DELETE.
    try:
        resource = await client().get(target, resource_name)
        events = ical.parse_resource(resource.ics)
        master = events[0]
        if "RRULE" not in master:
            return _error_result(
                "That id names an occurrence, but the event on the server is not "
                "recurring. Re-run search_events to get a current id."
            )

        master.add(
            "EXDATE",
            ical.recurrence_id_value(recurrence_id, master.get("DTSTART").dt),
        )

        # Drop any override for the occurrence being cancelled; leaving it would
        # resurrect the very instance the EXDATE removes.
        kept = [
            event
            for event in events
            if event is master
            or ical.recurrence_key(event.get("RECURRENCE-ID")) != recurrence_id
        ]
        ical.touch(master)

        tzids = {
            ical.event_to_dict(ev, calendar_id=target.id, resource_name=resource_name)[
                "timeZone"
            ]
            for ev in kept
        }
        body = ical.build_resource(kept, {tz for tz in tzids if tz})
        await client().put(target, resource_name, body, etag=resource.etag)
    except (CalDavError, ical.ICalError, ValueError) as exc:
        _record_write("delete_event", False, exc)
        return _error_result(f"Could not cancel the occurrence: {exc}")

    _record_write("delete_event", True)
    day = recurrence_id[:8]
    pretty = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 and day.isdigit() else day
    return _result(
        f"Cancelled the occurrence on {pretty}. The rest of the series is unchanged.",
        {"deleted": True, "occurrence": recurrence_id},
    )


# ----------------------------------------------------------------------
# Contacts
# ----------------------------------------------------------------------


def format_contact(contact: dict[str, Any]) -> str:
    """Render one contact as the human-readable text channel."""
    lines = [f"Name: {contact.get('name') or '(no name)'}"]

    if contact.get("kind") == "group":
        lines[0] += "  [group]"
    if contact.get("organization"):
        role = contact.get("jobTitle")
        lines.append(f"Organization: {contact['organization']}" + (f" ({role})" if role else ""))

    labeled = contact.get("labeled") or {}

    def render(field: str, heading: str) -> None:
        detailed = {entry["value"]: entry.get("label") for entry in labeled.get(field, [])}
        for value in contact.get(field) or []:
            label = detailed.get(value)
            lines.append(f"{heading}: {value}" + (f"  ({label})" if label else ""))

    render("emails", "Email")
    render("phones", "Phone")
    render("urls", "URL")

    for address in contact.get("addresses") or []:
        label = address.get("label")
        lines.append(f"Address: {address['formatted']}" + (f"  ({label})" if label else ""))

    if contact.get("birthday"):
        lines.append(f"Birthday: {contact['birthday']}")
    if contact.get("notes"):
        collapsed = " ".join(contact["notes"].split())
        lines.append(f"Notes: {collapsed[:300]}{'...' if len(collapsed) > 300 else ''}")

    for key, values in (contact.get("other") or {}).items():
        lines.append(f"{key}: {', '.join(values)}")

    lines.append(f"ID: {contact.get('id')}")
    return "\n".join(lines)


async def _load_contact(contact_id: str):
    """Resolve a contact id to ``(book, card, parsed)`` or raise."""
    book_id, resource, _ = ids.decode(contact_id, kind="contact")
    book = await contacts().address_book(book_id)
    card = await contacts().get(book, resource)
    return book, card, vcard.parse(card.vcard)


@mcp.tool
async def list_address_books() -> ToolResult:
    """List the user's address books, with their ids.

    Most iCloud accounts have exactly one. Use this only when a contact tool
    needs an explicit addressBookId.
    """
    try:
        books = await contacts().address_books()
    except (carddav.CardDavError, ValueError) as exc:
        return _error_result(str(exc))

    if not books:
        return _error_result("The account has no address books.")

    items = [
        {"id": b.id, "name": b.name, "readOnly": b.read_only, "isDefault": i == 0}
        for i, b in enumerate(books)
    ]
    text = "\n".join(
        f"{b['name']} ({b['id']})" + (" [read-only]" if b["readOnly"] else "")
        for b in items
    )
    return _result(text, {"items": items, "count": len(items)})


@mcp.tool
async def search_contacts(
    query: str | None = None,
    limit: int | None = None,
    addressBookId: str | None = None,
) -> ToolResult:
    """Search the user's address book.

    Returns contacts with everything on the card: name, email addresses, phone
    numbers, postal addresses, organization, job title, birthday, notes and
    URLs. Preferred entries come first, so emails[0] is the address to use
    unless the user says otherwise.

    Use this to turn a name into an email address before create_event, or into
    a phone number or postal address.

    Args:
        query: Text to match against names, email addresses, phone numbers,
            organizations, nicknames and notes. Omit to list the whole address
            book, which is large -- always pass a query unless the user really
            asked for everything.
        limit: Maximum number of results (default 10, max 50).
        addressBookId: Restrict to one address book. Defaults to all of them.
    """
    error = _validate_limit(limit)
    if error:
        return _error_result(error)
    effective_limit = DEFAULT_LIMIT if limit is None else limit

    needle = (query or "").strip()

    try:
        books = await contacts().address_books()
        if addressBookId:
            books = [b for b in books if b.id == addressBookId]
            if not books:
                return _error_result(
                    f"No address book with id {addressBookId!r}. Call "
                    "list_address_books to see the available ids."
                )

        found: list[dict[str, Any]] = []
        for book in books:
            for card in await contacts().search(book, needle or None):
                try:
                    found.append(
                        vcard.to_dict(
                            vcard.parse(card.vcard),
                            book_id=card.book_id,
                            resource_name=card.name,
                        )
                    )
                except vcard.VCardError as exc:
                    logger.warning("Skipping %s: %s", card.url, exc)
    except (carddav.CardDavError, ValueError) as exc:
        return _error_result(str(exc))

    found.sort(key=lambda c: (c.get("name") or "\uffff").lower())
    total = len(found)
    page = found[:effective_limit]

    if not page:
        text = (
            f"No contacts matching {needle!r}."
            if needle
            else "No contacts found."
        )
    else:
        header = f"Showing {len(page)} of {total} contacts\n\n" if total > len(page) else ""
        text = header + "\n\n---\n\n".join(format_contact(c) for c in page)

    return _result(
        text,
        {"items": page, "count": len(page), "total": total, "limit": effective_limit},
    )


@mcp.tool
async def create_contact(
    name: str,
    emails: list | None = None,
    phones: list | None = None,
    organization: str | None = None,
    birthday: str | None = None,
    notes: str | None = None,
    addressBookId: str | None = None,
) -> ToolResult:
    """Create a new contact in the address book. Returns the new contact's id.

    Args:
        name: Full name of the contact.
        emails: Email addresses. The first becomes the preferred one.
        phones: Phone numbers. The first becomes the preferred one.
        organization: Company or organization name.
        birthday: Birthday as an ISO date (YYYY-MM-DD). The year may be omitted
            as "0000" (e.g. 0000-03-15) for a birthday whose year is unknown.
        notes: Free-text notes about this contact.
        addressBookId: Address book to add it to. Defaults to the first
            writable one.
    """
    if not name or not name.strip():
        return _error_result("name is required.")

    emails = [str(e).strip() for e in (emails or []) if str(e).strip()]
    phones = [str(p).strip() for p in (phones or []) if str(p).strip()]
    bad = [e for e in emails if "@" not in e]
    if bad:
        return _error_result(
            "These do not look like email addresses: " + ", ".join(repr(e) for e in bad) + "."
        )

    try:
        book = (
            await contacts().address_book(addressBookId)
            if addressBookId
            else await contacts().default_book()
        )
    except (carddav.CardDavError, ValueError) as exc:
        return _error_result(str(exc))

    if book.read_only:
        return _error_result(f"The address book {book.name!r} is read-only.")

    uid = vcard.new_uid()
    try:
        body = vcard.build(
            uid=uid,
            name=name.strip(),
            emails=emails,
            phones=phones,
            organization=(organization or "").strip(),
            birthday=(birthday or "").strip(),
            notes=(notes or "").strip(),
        )
    except (vcard.VCardError, ValueError) as exc:
        return _error_result(str(exc))

    resource = f"{uid}.vcf"
    try:
        await contacts().put(book, resource, body, create=True)
    except carddav.CardDavError as exc:
        _record_write("create_contact", False, exc)
        return _error_result(f"Could not create the contact: {exc}")

    _record_write("create_contact", True)
    payload = vcard.to_dict(vcard.parse(body), book_id=book.id, resource_name=resource)
    return _result(
        f"Created contact in {book.name}.\n\n{format_contact(payload)}",
        {"id": payload["id"], "contact": payload},
    )


@mcp.tool
async def update_contact(
    id: str,
    name: str | None = None,
    addEmails: list | None = None,
    removeEmails: list | None = None,
    addPhones: list | None = None,
    removePhones: list | None = None,
    organization: str | None = None,
    birthday: str | None = None,
    notes: str | None = None,
) -> ToolResult:
    """Update fields on an existing contact.

    Scalar fields (name, organization, notes, birthday) replace. Emails and
    phones use add/remove deltas so a partial edit does not drop existing
    entries. Use create_contact for new contacts.

    Everything else on the card is preserved untouched, including photos,
    social profiles, related names and any labels.

    Args:
        id: ID of the contact to update, as returned by search_contacts.
        name: New full name.
        addEmails: Email addresses to add. Duplicates are skipped.
        removeEmails: Email addresses to remove, matched case-insensitively.
            Entries that are not present are ignored.
        addPhones: Phone numbers to add.
        removePhones: Phone numbers to remove, matched exactly.
        organization: Company or organization name. Empty string clears it.
        birthday: Birthday as an ISO date (YYYY-MM-DD). Empty string clears it.
        notes: Free-text notes, replacing what is there. Empty string clears it.
    """
    try:
        book_id, resource, _ = ids.decode(id, kind="contact")
    except ids.BadId as exc:
        return _error_result(str(exc))

    try:
        book = await contacts().address_book(book_id)
        if book.read_only:
            return _error_result(f"The address book {book.name!r} is read-only.")
        card = await contacts().get(book, resource)
        parsed = vcard.parse(card.vcard)
    except (carddav.CardDavError, vcard.VCardError, ValueError) as exc:
        return _error_result(str(exc))

    if name is not None:
        if not name.strip():
            return _error_result("name cannot be empty.")
        vcard.set_text(parsed, "FN", name.strip())
    if organization is not None:
        vcard.set_org(parsed, organization.strip())
    if birthday is not None:
        vcard.set_text(parsed, "BDAY", birthday.strip())
    if notes is not None:
        vcard.set_text(parsed, "NOTE", notes.strip())

    added_emails = vcard.add_values(
        parsed, "EMAIL", [str(e) for e in (addEmails or [])], ["INTERNET"]
    )
    removed_emails = vcard.remove_values(
        parsed, "EMAIL", [str(e) for e in (removeEmails or [])]
    )
    added_phones = vcard.add_values(
        parsed, "TEL", [str(p) for p in (addPhones or [])], ["CELL", "VOICE"]
    )
    removed_phones = vcard.remove_values(
        parsed, "TEL", [str(p) for p in (removePhones or [])]
    )

    vcard.touch(parsed)

    try:
        await contacts().put(book, resource, parsed.serialize(), etag=card.etag)
    except carddav.CardDavError as exc:
        _record_write("update_contact", False, exc)
        return _error_result(f"Could not update the contact: {exc}")

    _record_write("update_contact", True)
    payload = vcard.to_dict(parsed, book_id=book.id, resource_name=resource)
    return _result(
        f"Updated contact.\n\n{format_contact(payload)}",
        {
            "contact": payload,
            "addedEmails": added_emails,
            "removedEmails": removed_emails,
            "addedPhones": added_phones,
            "removedPhones": removed_phones,
        },
    )


@mcp.tool
async def delete_contact(id: str) -> ToolResult:
    """Delete a contact from the address book.

    Args:
        id: The contact ID to delete, as returned by search_contacts.
    """
    try:
        book_id, resource, _ = ids.decode(id, kind="contact")
    except ids.BadId as exc:
        return _error_result(str(exc))

    try:
        book = await contacts().address_book(book_id)
        if book.read_only:
            return _error_result(f"The address book {book.name!r} is read-only.")
    except (carddav.CardDavError, ValueError) as exc:
        return _error_result(str(exc))

    try:
        await contacts().delete(book, resource)
    except carddav.NotFound:
        _record_write("delete_contact", True)
        return _result(
            "That contact no longer exists; nothing to delete.",
            {"deleted": False, "reason": "not-found"},
        )
    except carddav.CardDavError as exc:
        _record_write("delete_contact", False, exc)
        return _error_result(f"Could not delete the contact: {exc}")

    _record_write("delete_contact", True)
    return _result("Deleted the contact.", {"deleted": True})


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------


# /health is unauthenticated, and the hostname is public within hours of going
# live, so anything on the internet can poll it as fast as it likes. Each miss
# costs the full principal -> home-set -> calendars walk against iCloud, and a
# burst trips throttling that applies to the whole Apple ID -- reads included,
# for tens of minutes. Without a cache an outsider can turn the health endpoint
# into an outage of every calendar tool.
HEALTH_TTL_SECONDS = 30.0

_health_cache: tuple[float, tuple[bool, int | None, str | None]] | None = None
_health_lock = asyncio.Lock()


async def _probe() -> tuple[bool, int | None, str | None]:
    """Count the account's event calendars, at most once per ``HEALTH_TTL_SECONDS``.

    Returns ``(reachable, calendars, error_class)``. The lock is held across the
    request on purpose: it collapses a concurrent burst into one walk rather
    than letting every caller that arrives before the cache fills start its own.
    """
    global _health_cache
    async with _health_lock:
        now = time.time()
        if _health_cache and now - _health_cache[0] < HEALTH_TTL_SECONDS:
            return _health_cache[1]

        try:
            result = (True, len(await client().calendars()), None)
        except Exception as exc:  # the class is enough; the message may leak
            result = (False, None, type(exc).__name__)

        _health_cache = (time.time(), result)
        return result


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Unauthenticated health report, for monitoring a remote deployment.

    Deliberately publishes no account detail -- not the Apple ID, not calendar
    names. A tunnel hostname shows up in certificate transparency logs within
    hours and gets scanned, so this endpoint is effectively public.

    ``python_version`` is here for the same reason things-mcp publishes it: on
    macOS a privacy grant is bound to the interpreter's versioned path, and an
    upgrade silently invalidates it. Watching the version predicts the breakage.
    """
    reachable, calendars, detail = await _probe()

    return JSONResponse(
        {
            "status": "ok" if reachable and calendars else "degraded",
            "caldav_reachable": reachable,
            "event_calendars": calendars,
            "error": detail,
            "python_version": platform.python_version(),
            "last_write": {
                "at": (
                    datetime.fromtimestamp(_last_write["at"]).isoformat()
                    if _last_write["at"]
                    else None
                ),
                "ok": _last_write["ok"],
                "action": _last_write["action"],
                "error": _last_write["error"],
            },
        }
    )


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def _is_loopback(host: str) -> bool:
    """Whether binding to ``host`` keeps the port on this machine."""
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return host.strip().lower() in ("localhost", "")


def main():
    """Run the server on the transport the environment selects."""
    # Read transport settings here rather than at import time so that a service
    # manager and the tests can set the environment before calling main().
    transport = os.environ.get("DAV_MCP_TRANSPORT", "stdio")
    if transport == "http":
        from .auth import build_auth

        host = os.environ.get("DAV_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("DAV_MCP_PORT", "18790"))
        # Authentication applies to the HTTP transport only; stdio inherits its
        # security from local execution.
        mcp.auth = build_auth()
        if mcp.auth is None and not _is_loopback(host):
            # Not fatal: a host behind a tunnel that does its own authentication
            # is a legitimate setup, and refusing to start would break it. But
            # the combination otherwise hands the account's calendars and
            # contacts -- readable and writable, with the ability to send mail
            # as the user -- to anyone who can reach the port.
            logger.warning(
                "Serving on %s with DAV_MCP_AUTH unset: this port is reachable "
                "beyond this machine and anyone who finds it can read and write "
                "the account's calendars and contacts. Set DAV_MCP_AUTH=password "
                "unless something in front of the server is authenticating.",
                host,
            )
        # Stateless: a fresh transport per request, so there is no session for a
        # client to lose. Remote clients dial from a pool of addresses, and a
        # request arriving from a different address than the one that opened the
        # session is rejected with a 400. Nothing here needs session state.
        stateless = (
            os.environ.get("DAV_MCP_STATELESS", "true").strip().lower() != "false"
        )
        mcp.run(transport="http", host=host, port=port, stateless_http=stateless)
    else:
        mcp.run()


__all__ = ["main", "mcp", "format_event"]
