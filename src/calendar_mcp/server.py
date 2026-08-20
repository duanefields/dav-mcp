"""The MCP server: tool definitions, health endpoint, and transport selection.

The tool surface mirrors the Fastmail calendar tools on purpose. ``compose_event``
is deliberately absent -- it stages an event into a confirmation widget that only
exists inside claude.ai, and has no meaning in a generic MCP client.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import time
from datetime import datetime, timedelta
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from starlette.responses import JSONResponse

from . import caldav, ical, ids
from .caldav import CalDavClient, CalDavError
from .dates import (
    DateError,
    has_explicit_offset,
    local_zone,
    parse_duration,
    parse_when,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP("Calendar")

MAX_LIMIT = 50
DEFAULT_LIMIT = 10

# Mirrors the Fastmail defaults: far enough back to catch "when did I last see
# them", far enough forward to catch anything already scheduled.
DEFAULT_AFTER = "3 months ago"
DEFAULT_BEFORE = "12 months from now"

_client: CalDavClient | None = None

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


def _record_write(action: str, ok: bool, error: str | None = None) -> None:
    _last_write.update({"at": time.time(), "ok": ok, "error": error, "action": action})


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
    """
    if not title or not title.strip():
        return _error_result("title is required.")

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
        uid = str(event["UID"])
        body = ical.build_resource([event], {tzid} if tzid else set())
    except (ical.ICalError, ValueError) as exc:
        return _error_result(str(exc))

    name = f"{uid}.ics"
    try:
        await client().put(target, name, body, create=True)
    except CalDavError as exc:
        _record_write("create_event", False, str(exc))
        return _error_result(f"Could not create the event: {exc}")

    _record_write("create_event", True)
    event_id = ids.encode(target.id, name)
    payload = ical.event_to_dict(
        event,
        calendar_id=target.id,
        resource_name=name,
        calendar_color=target.color,
    )
    return _result(
        f"Created event in {target.name}.\n\n{format_event(payload)}",
        {"id": event_id, "event": payload},
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
    """
    try:
        calendar_id, resource_name, recurrence_id = ids.decode(id)
    except ids.BadEventId as exc:
        return _error_result(str(exc))

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
        _record_write("update_event", False, str(exc))
        return _error_result(f"Could not update the event: {exc}")

    _record_write("update_event", True)
    payload = ical.event_to_dict(
        event,
        calendar_id=target.id,
        resource_name=resource_name,
        calendar_color=target.color,
    )
    return _result(f"Updated event.\n\n{format_event(payload)}", {"event": payload})


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
            _record_write("delete_event", False, str(exc))
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
        _record_write("delete_event", False, str(exc))
        return _error_result(f"Could not cancel the occurrence: {exc}")

    _record_write("delete_event", True)
    day = recurrence_id[:8]
    pretty = f"{day[:4]}-{day[4:6]}-{day[6:8]}" if len(day) == 8 and day.isdigit() else day
    return _result(
        f"Cancelled the occurrence on {pretty}. The rest of the series is unchanged.",
        {"deleted": True, "occurrence": recurrence_id},
    )


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------


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
    reachable = False
    calendars = None
    detail = None

    try:
        found = await client().calendars()
        reachable = True
        calendars = len(found)
    except Exception as exc:  # surfacing the class is enough; the text may leak
        detail = type(exc).__name__

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


def main():
    """Run the server on the transport the environment selects."""
    # Read transport settings here rather than at import time so that a service
    # manager and the tests can set the environment before calling main().
    transport = os.environ.get("CALENDAR_MCP_TRANSPORT", "stdio")
    if transport == "http":
        from .auth import build_auth

        host = os.environ.get("CALENDAR_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("CALENDAR_MCP_PORT", "18790"))
        # Authentication applies to the HTTP transport only; stdio inherits its
        # security from local execution.
        mcp.auth = build_auth()
        # Stateless: a fresh transport per request, so there is no session for a
        # client to lose. Remote clients dial from a pool of addresses, and a
        # request arriving from a different address than the one that opened the
        # session is rejected with a 400. Nothing here needs session state.
        stateless = (
            os.environ.get("CALENDAR_MCP_STATELESS", "true").strip().lower() != "false"
        )
        mcp.run(transport="http", host=host, port=port, stateless_http=stateless)
    else:
        mcp.run()


__all__ = ["main", "mcp", "format_event"]
