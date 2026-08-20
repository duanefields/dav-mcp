"""Interval arithmetic for free-time search.

Pure functions over aware datetimes: no network, no clock, no iCalendar. The
edge cases here are the ones that make a scheduling tool wrong in ways nobody
notices until a meeting is double-booked.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from dav_mcp import availability

CHICAGO = ZoneInfo("America/Chicago")


def at(day, hour, minute=0, tz=CHICAGO):
    return datetime(2026, 9, day, hour, minute, tzinfo=tz)


class TestMerge:
    def test_overlapping_intervals_become_one(self):
        merged = availability.merge([(at(14, 9), at(14, 11)), (at(14, 10), at(14, 12))])
        assert merged == [(at(14, 9), at(14, 12))]

    def test_touching_intervals_become_one(self):
        # Back-to-back meetings leave no gap between them.
        merged = availability.merge([(at(14, 9), at(14, 10)), (at(14, 10), at(14, 11))])
        assert merged == [(at(14, 9), at(14, 11))]

    def test_a_contained_interval_is_absorbed(self):
        merged = availability.merge([(at(14, 9), at(14, 17)), (at(14, 12), at(14, 13))])
        assert merged == [(at(14, 9), at(14, 17))]

    def test_separate_intervals_stay_separate(self):
        given = [(at(14, 9), at(14, 10)), (at(14, 14), at(14, 15))]
        assert availability.merge(given) == given

    def test_input_order_does_not_matter(self):
        given = [(at(14, 14), at(14, 15)), (at(14, 9), at(14, 10))]
        assert availability.merge(given) == sorted(given)

    def test_zero_length_intervals_are_dropped(self):
        assert availability.merge([(at(14, 9), at(14, 9))]) == []

    def test_nothing_merges_to_nothing(self):
        assert availability.merge([]) == []


class TestInvert:
    def test_a_meeting_splits_the_window(self):
        free = availability.invert(
            [(at(14, 12), at(14, 13))], (at(14, 9), at(14, 17))
        )
        assert free == [(at(14, 9), at(14, 12)), (at(14, 13), at(14, 17))]

    def test_an_empty_calendar_leaves_the_whole_window(self):
        window = (at(14, 9), at(14, 17))
        assert availability.invert([], window) == [window]

    def test_a_full_day_leaves_nothing(self):
        free = availability.invert([(at(14, 8), at(14, 18))], (at(14, 9), at(14, 17)))
        assert free == []

    def test_busy_time_outside_the_window_is_ignored(self):
        window = (at(14, 9), at(14, 17))
        free = availability.invert([(at(13, 9), at(13, 17))], window)
        assert free == [window]

    def test_a_meeting_overlapping_the_start_trims_it(self):
        free = availability.invert([(at(14, 8), at(14, 10))], (at(14, 9), at(14, 17)))
        assert free == [(at(14, 10), at(14, 17))]

    def test_a_meeting_overlapping_the_end_trims_it(self):
        free = availability.invert([(at(14, 16), at(14, 20))], (at(14, 9), at(14, 17)))
        assert free == [(at(14, 9), at(14, 16))]


class TestWorkingWindows:
    def test_one_window_per_weekday(self):
        windows = availability.working_windows(
            at(14, 0), at(18, 23, 59),        # Mon 14th through Fri 18th
            tz=CHICAGO, day_start=time(9), day_end=time(17), weekdays=set(range(5)),
        )
        assert len(windows) == 5
        assert all(w[0].hour == 9 and w[1].hour == 17 for w in windows)

    def test_weekends_are_excluded_when_not_asked_for(self):
        windows = availability.working_windows(
            at(19, 0), at(20, 23, 59),        # Sat 19th and Sun 20th
            tz=CHICAGO, day_start=time(9), day_end=time(17), weekdays=set(range(5)),
        )
        assert windows == []

    def test_weekends_are_included_when_asked_for(self):
        windows = availability.working_windows(
            at(19, 0), at(20, 23, 59),
            tz=CHICAGO, day_start=time(9), day_end=time(17), weekdays=set(range(7)),
        )
        assert len(windows) == 2

    def test_the_range_clips_the_first_and_last_windows(self):
        # Starting at 11am must not offer 9am on the first day.
        windows = availability.working_windows(
            at(14, 11), at(14, 15),
            tz=CHICAGO, day_start=time(9), day_end=time(17), weekdays=set(range(5)),
        )
        assert windows == [(at(14, 11), at(14, 15))]

    def test_days_are_walked_in_the_given_zone(self):
        # Late evening in Chicago is already the next day in UTC. Walking days
        # in UTC would put the working window on the wrong date.
        late = datetime(2026, 9, 14, 23, 0, tzinfo=CHICAGO)
        windows = availability.working_windows(
            late, late + timedelta(hours=2),
            tz=CHICAGO, day_start=time(9), day_end=time(17), weekdays=set(range(7)),
        )
        assert windows == []          # 23:00-01:00 is outside 09:00-17:00

    def test_a_backwards_working_day_is_rejected(self):
        with pytest.raises(ValueError):
            availability.working_windows(
                at(14, 0), at(14, 23),
                tz=CHICAGO, day_start=time(17), day_end=time(9), weekdays={0},
            )


class TestSlots:
    def test_finds_a_gap_between_meetings(self):
        windows = [(at(14, 9), at(14, 17))]
        busy = [(at(14, 9), at(14, 12)), (at(14, 13), at(14, 17))]
        assert availability.slots(windows, busy, timedelta(hours=1)) == [
            (at(14, 12), at(14, 13))
        ]

    def test_a_gap_shorter_than_needed_is_not_offered(self):
        windows = [(at(14, 9), at(14, 17))]
        busy = [(at(14, 9), at(14, 12)), (at(14, 12, 30), at(14, 17))]
        assert availability.slots(windows, busy, timedelta(hours=1)) == []

    def test_a_gap_exactly_long_enough_is_offered(self):
        windows = [(at(14, 9), at(14, 17))]
        busy = [(at(14, 9), at(14, 12)), (at(14, 13), at(14, 17))]
        assert availability.slots(windows, busy, timedelta(hours=1))

    def test_the_full_gap_is_reported_not_just_the_requested_length(self):
        # Knowing a three-hour opening exists beats being told an hour fits.
        windows = [(at(14, 9), at(14, 17))]
        busy = [(at(14, 9), at(14, 14))]
        assert availability.slots(windows, busy, timedelta(hours=1)) == [
            (at(14, 14), at(14, 17))
        ]

    def test_results_are_chronological_across_days(self):
        windows = [(at(14, 9), at(14, 17)), (at(15, 9), at(15, 17))]
        found = availability.slots(windows, [], timedelta(hours=1))
        assert found[0][0] < found[1][0]

    def test_the_limit_stops_the_search_early(self):
        windows = [(at(day, 9), at(day, 17)) for day in (14, 15, 16, 17, 18)]
        assert len(availability.slots(windows, [], timedelta(hours=1), limit=2)) == 2

    def test_a_non_positive_duration_is_rejected(self):
        with pytest.raises(ValueError):
            availability.slots([(at(14, 9), at(14, 17))], [], timedelta(0))

    def test_overlapping_meetings_do_not_manufacture_a_gap(self):
        # Two overlapping meetings must merge, not leave phantom free time.
        windows = [(at(14, 9), at(14, 17))]
        busy = [(at(14, 9), at(14, 13)), (at(14, 11), at(14, 17))]
        assert availability.slots(windows, busy, timedelta(minutes=30)) == []
