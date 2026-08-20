"""Date and duration handling shared by the tool layer and the CalDAV layer.

Two jobs live here:

* Parsing the ``after``/``before`` arguments, which accept ISO 8601 or the same
  relative expressions the Fastmail calendar tools accept ("today", "tomorrow",
  "3 weeks from now"). Models reach for those constantly, and rejecting them
  would push the arithmetic into the model where it gets it wrong.
* Converting between an ISO 8601 duration string and a ``timedelta``, since the
  tool surface expresses length as ``duration`` while iCalendar expresses it as
  ``DTEND``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# "3 days ago", "2 weeks from now", "1 month from now"
_RELATIVE = re.compile(
    r"^(?P<n>\d+)\s+(?P<unit>day|days|week|weeks|month|months|year|years)\s+"
    r"(?P<dir>ago|from\s+now)$"
)

_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

_DURATION = re.compile(
    r"^(?P<sign>[+-])?P"
    r"(?:(?P<weeks>\d+)W)?"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


class DateError(ValueError):
    """A date or duration argument could not be understood."""


def local_zone() -> ZoneInfo:
    """The IANA zone to assume when a caller omits ``timeZone``.

    ``DAV_MCP_TIMEZONE`` wins if set. Otherwise this reads the
    ``/etc/localtime`` symlink, which is how macOS and Linux both record the
    zone by name -- the stdlib only exposes the current *offset*, and an
    abbreviation like "CDT" is not something ``ZoneInfo`` can take.

    Resolved per call rather than cached, because the process is long-lived and
    a host can change zone under it.
    """
    configured = os.environ.get("DAV_MCP_TIMEZONE", "").strip()
    if configured:
        try:
            return ZoneInfo(configured)
        except Exception:
            logger.warning(
                "DAV_MCP_TIMEZONE=%r is not a known IANA zone; ignoring it.",
                configured,
            )

    try:
        link = os.readlink("/etc/localtime")
        if "/zoneinfo/" in link:
            return ZoneInfo(link.split("/zoneinfo/", 1)[1])
    except OSError:
        pass

    return ZoneInfo("UTC")


def parse_when(value: str | None, *, default: datetime, tz: ZoneInfo) -> datetime:
    """Resolve an ``after``/``before`` argument to an aware datetime.

    Accepts ISO 8601 (with or without a time part, with or without an offset)
    and the relative vocabulary. A naive ISO value is interpreted in ``tz``,
    matching how a user means "3pm" when they type it.
    """
    if value is None or not value.strip():
        return default

    text = " ".join(value.strip().lower().split())

    now = datetime.now(tz)
    if text == "now":
        return now
    if text == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "tomorrow":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    if text == "yesterday":
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)

    match = _RELATIVE.match(text)
    if match:
        n = int(match.group("n"))
        unit = match.group("unit").rstrip("s")
        days = n * _UNIT_DAYS[unit]
        delta = timedelta(days=days)
        return now - delta if match.group("dir") == "ago" else now + delta

    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        try:
            parsed = datetime.combine(date.fromisoformat(value.strip()), datetime.min.time())
        except ValueError:
            raise DateError(
                f"Could not understand the date {value!r}. Use ISO 8601 "
                '("2026-03-15" or "2026-03-15T14:00:00") or a relative expression '
                '("today", "tomorrow", "2 weeks from now", "3 days ago").'
            ) from None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed


def has_explicit_offset(value: str | None) -> bool:
    """Whether an ISO 8601 string pinned itself to a UTC offset.

    This decides what ``timeZone`` means alongside a ``start``. Given a bare
    "2026-03-15T14:00:00", the user means 2pm *in that zone*. Given
    "2026-03-15T14:00:00+00:00" they named an instant, and reinterpreting its
    wall clock in another zone would silently move the event.
    """
    if not value:
        return False
    text = value.strip()
    if text.endswith(("Z", "z")):
        return True
    # Only an offset after the time part counts; a date's own hyphens do not.
    time_part = text.partition("T")[2]
    return "+" in time_part or "-" in time_part


def parse_duration(value: str | None) -> timedelta:
    """Parse an ISO 8601 duration such as ``PT1H``, ``PT30M`` or ``P2D``."""
    if value is None or not value.strip():
        raise DateError("duration is required")
    match = _DURATION.match(value.strip().upper())
    # The regex makes every component optional, so "P" and "PT" match it while
    # carrying no length at all. Requiring at least one component rejects them
    # instead of silently returning a zero-length event.
    if not match or not any(
        value for key, value in match.groupdict().items() if key != "sign"
    ):
        raise DateError(
            f"Could not understand the duration {value!r}. Use ISO 8601, for "
            'example "PT1H" (one hour), "PT30M" (thirty minutes) or "P1D" (one day).'
        )
    parts = {k: int(v) for k, v in match.groupdict().items() if k != "sign" and v}
    delta = timedelta(
        weeks=parts.get("weeks", 0),
        days=parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )
    return -delta if match.group("sign") == "-" else delta


def format_duration(delta: timedelta) -> str:
    """Render a ``timedelta`` the way the tool surface reports it.

    Whole days become ``P<n>D`` because that is what an all-day event means;
    everything else becomes a ``PT...`` time duration. Sub-second precision is
    dropped -- no calendar system carries it.
    """
    total = int(delta.total_seconds())
    if total <= 0:
        return "PT0S"
    if total % 86400 == 0:
        return f"P{total // 86400}D"

    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    out = "PT"
    if hours:
        out += f"{hours}H"
    if minutes:
        out += f"{minutes}M"
    if seconds:
        out += f"{seconds}S"
    return out


def to_utc_stamp(moment: datetime) -> str:
    """Format an aware datetime as the ``YYYYMMDDTHHMMSSZ`` CalDAV wants."""
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
