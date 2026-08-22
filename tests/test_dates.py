"""Date, duration and offset parsing.

These are pure functions with no network, and they are where a quiet mistake
does the most damage: an event silently landing an hour or a day off is much
harder to notice than one that fails to save.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dav_mcp.dates import (
    DateError,
    format_duration,
    has_explicit_offset,
    parse_duration,
    parse_when,
    to_utc_stamp,
)

CHICAGO = ZoneInfo("America/Chicago")


class TestParseWhen:
    def test_returns_the_default_when_nothing_is_supplied(self):
        default = datetime(2026, 1, 1, tzinfo=CHICAGO)
        assert parse_when(None, default=default, tz=CHICAGO) is default
        assert parse_when("   ", default=default, tz=CHICAGO) is default

    def test_today_is_midnight_not_now(self):
        result = parse_when("today", default=None, tz=CHICAGO)
        assert (result.hour, result.minute, result.second) == (0, 0, 0)

    def test_tomorrow_is_one_day_after_today(self):
        today = parse_when("today", default=None, tz=CHICAGO)
        assert parse_when("tomorrow", default=None, tz=CHICAGO) - today == timedelta(days=1)

    @pytest.mark.parametrize(
        "value",
        ["99999999 years from now", "9" * 400 + " days ago", "999999999 weeks ago"],
    )
    def test_an_unrepresentable_relative_date_is_a_date_error(self, value):
        # The count is unbounded in the pattern and both the timedelta and the
        # addition overflow. OverflowError is not a ValueError, so before this
        # it escaped the tools' `except DateError` and reached the caller as an
        # opaque crash rather than something the model could act on.
        with pytest.raises(DateError):
            parse_when(value, default=None, tz=CHICAGO)

    def test_relative_expressions_go_both_directions(self):
        now = parse_when("now", default=None, tz=CHICAGO)
        ahead = parse_when("2 weeks from now", default=None, tz=CHICAGO)
        behind = parse_when("3 days ago", default=None, tz=CHICAGO)
        assert timedelta(days=13) < ahead - now < timedelta(days=15)
        assert timedelta(days=2) < now - behind < timedelta(days=4)

    def test_case_and_spacing_do_not_matter(self):
        # Models produce "From Now" and doubled spaces often enough to matter.
        assert parse_when("2  WEEKS   From Now", default=None, tz=CHICAGO) is not None

    def test_a_naive_iso_value_is_read_in_the_given_zone(self):
        result = parse_when("2026-03-15T14:00:00", default=None, tz=CHICAGO)
        assert result.tzinfo is CHICAGO
        assert result.hour == 14

    def test_a_date_only_value_becomes_midnight(self):
        result = parse_when("2026-03-15", default=None, tz=CHICAGO)
        assert (result.hour, result.minute) == (0, 0)
        assert result.date() == datetime(2026, 3, 15).date()

    def test_an_explicit_offset_is_preserved_rather_than_relabeled(self):
        result = parse_when("2026-03-15T14:00:00+00:00", default=None, tz=CHICAGO)
        assert result.utcoffset() == timedelta(0)

    def test_nonsense_names_what_it_would_have_accepted(self):
        with pytest.raises(DateError) as excinfo:
            parse_when("next tuesdayish", default=None, tz=CHICAGO)
        assert "relative expression" in str(excinfo.value)


class TestHasExplicitOffset:
    @pytest.mark.parametrize(
        "value",
        ["2026-03-15T14:00:00Z", "2026-03-15T14:00:00+02:00", "2026-03-15T14:00:00-05:00"],
    )
    def test_detects_a_pinned_instant(self, value):
        assert has_explicit_offset(value) is True

    @pytest.mark.parametrize("value", ["2026-03-15T14:00:00", "2026-03-15", "", None])
    def test_a_bare_wall_clock_is_not_pinned(self, value):
        # The hyphens in the date must not read as a negative offset.
        assert has_explicit_offset(value) is False


class TestDurations:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("PT1H", timedelta(hours=1)),
            ("PT30M", timedelta(minutes=30)),
            ("P1D", timedelta(days=1)),
            ("P2D", timedelta(days=2)),
            ("PT1H30M", timedelta(hours=1, minutes=30)),
            ("P1W", timedelta(weeks=1)),
        ],
    )
    def test_parses_the_forms_the_tool_surface_documents(self, text, expected):
        assert parse_duration(text) == expected

    @pytest.mark.parametrize("text", ["", None, "1 hour", "PT", "P", "banana"])
    def test_rejects_what_it_cannot_honor(self, text):
        with pytest.raises(DateError):
            parse_duration(text)

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (timedelta(hours=1), "PT1H"),
            (timedelta(minutes=15), "PT15M"),
            (timedelta(days=1), "P1D"),
            (timedelta(days=3), "P3D"),
            (timedelta(hours=1, minutes=45), "PT1H45M"),
            (timedelta(0), "PT0S"),
        ],
    )
    def test_formats_back_to_the_documented_forms(self, delta, expected):
        assert format_duration(delta) == expected

    def test_whole_days_prefer_the_day_form(self):
        # P1D and PT24H are the same length, but only one reads as "all day".
        assert format_duration(timedelta(hours=24)) == "P1D"

    def test_round_trips(self):
        for text in ("PT1H", "PT30M", "P1D", "PT2H15M"):
            assert format_duration(parse_duration(text)) == text


class TestToUtcStamp:
    def test_converts_to_the_compact_utc_form_caldav_requires(self):
        moment = datetime(2026, 3, 15, 9, 30, tzinfo=CHICAGO)
        assert to_utc_stamp(moment) == "20260315T143000Z"
