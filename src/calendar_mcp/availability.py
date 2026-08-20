"""Working out when someone is free.

Kept apart from the tool layer because it is pure arithmetic over intervals and
deserves to be tested as such: no network, no iCalendar, no clock. Everything
here takes and returns aware datetimes.

The interesting decisions are not the interval maths, they are which events
count as busy at all. Getting that wrong makes the tool useless in opposite
directions -- count everything and a calendar with a birthday on it has no free
time; count nothing and it offers slots during real meetings.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

Interval = tuple[datetime, datetime]


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Collapse overlapping and touching intervals into a minimal set."""
    ordered = sorted((iv for iv in intervals if iv[1] > iv[0]), key=lambda iv: iv[0])
    if not ordered:
        return []

    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:                    # touching counts as overlapping
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def invert(busy: list[Interval], window: Interval) -> list[Interval]:
    """The gaps left in ``window`` once ``busy`` is removed."""
    window_start, window_end = window
    free: list[Interval] = []
    cursor = window_start

    for start, end in merge(busy):
        if end <= window_start or start >= window_end:
            continue
        if start > cursor:
            free.append((cursor, start))
        cursor = max(cursor, end)

    if cursor < window_end:
        free.append((cursor, window_end))
    return free


def working_windows(
    start: datetime,
    end: datetime,
    *,
    tz: ZoneInfo,
    day_start: time,
    day_end: time,
    weekdays: set[int],
) -> list[Interval]:
    """Split a range into per-day windows bounded by working hours.

    Days are walked in ``tz`` rather than UTC, because "9 to 5" is a local
    notion and a UTC-based walk silently shifts the working day for anyone not
    on UTC.
    """
    if day_end <= day_start:
        raise ValueError("dayEnd must be later than dayStart")

    windows: list[Interval] = []
    day: date = start.astimezone(tz).date()
    last: date = end.astimezone(tz).date()

    while day <= last:
        if day.weekday() in weekdays:
            opens = datetime.combine(day, day_start, tzinfo=tz)
            closes = datetime.combine(day, day_end, tzinfo=tz)
            window = (max(opens, start), min(closes, end))
            if window[1] > window[0]:
                windows.append(window)
        day += timedelta(days=1)

    return windows


def slots(
    windows: list[Interval],
    busy: list[Interval],
    duration: timedelta,
    *,
    limit: int | None = None,
) -> list[Interval]:
    """Every gap long enough to hold ``duration``, in chronological order.

    A gap is reported at its full length rather than trimmed to ``duration``:
    knowing a two-hour opening exists is more useful than being told only that
    a one-hour meeting fits somewhere inside it.
    """
    if duration <= timedelta(0):
        raise ValueError("duration must be positive")

    found: list[Interval] = []
    for window in windows:
        for gap in invert(busy, window):
            if gap[1] - gap[0] >= duration:
                found.append(gap)
                if limit is not None and len(found) >= limit:
                    return found
    return found
