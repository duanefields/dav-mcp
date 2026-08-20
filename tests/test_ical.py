"""Reading and writing VEVENTs.

The fixtures reproduce the exact shapes iCloud returns, with the identities
replaced. That fidelity is the point: every bug this file guards against was
found by comparing what iCloud actually sent against what the code assumed.
"""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from calendar_mcp import ical, ids

CHICAGO = ZoneInfo("America/Chicago")

TIMED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:11111111-1111-4111-8111-111111111111
SUMMARY:Dentist
DESCRIPTION:bring the card
LOCATION:Main Street Clinic
DTSTART;TZID=America/Chicago:20260827T100000
DTEND;TZID=America/Chicago:20260827T110000
END:VEVENT
END:VCALENDAR
"""

ALL_DAY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:22222222-2222-4222-8222-222222222222
SUMMARY:First Day of School
DTSTART;VALUE=DATE:20260824
DTEND;VALUE=DATE:20260825
END:VEVENT
END:VCALENDAR
"""

# An expanded occurrence exactly as iCloud returns it: DTSTART in the event's
# own zone, but RECURRENCE-ID in UTC. Conflating the two was a real bug.
OCCURRENCE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:33333333-3333-4333-8333-333333333333
SUMMARY:Standup
DTSTART;TZID=America/Chicago:20260902T093000
DTEND;TZID=America/Chicago:20260902T094500
RECURRENCE-ID:20260902T143000Z
END:VEVENT
END:VCALENDAR
"""

WITH_PEOPLE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:seriesuid1234@google.com
SUMMARY:Book Club
DTSTART;TZID=America/Chicago:20260825T193000
DTEND;TZID=America/Chicago:20260825T213000
ORGANIZER;CN=Ada Organizer;EMAIL=ada@example.com:mailto:ada@example.com
ATTENDEE;CN=Sam Attendee;PARTSTAT=ACCEPTED;EMAIL=sam@example.net:mailto:sam@example.net
ATTENDEE;CN=Jo;PARTSTAT=NEEDS-ACTION;EMAIL=jo@example.org:mailto:jo@example.org
END:VEVENT
END:VCALENDAR
"""

RECURRING = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:rrule-1
SUMMARY:Standup
DTSTART;TZID=America/Chicago:20260901T093000
DTEND;TZID=America/Chicago:20260901T094500
RRULE:FREQ=WEEKLY;COUNT=6;BYDAY=MO,WE,FR
END:VEVENT
END:VCALENDAR
"""


def to_dict(ics, **kwargs):
    event = ical.parse_resource(ics)[0]
    kwargs.setdefault("calendar_id", "CAL")
    kwargs.setdefault("resource_name", "event.ics")
    return ical.event_to_dict(event, **kwargs)


class TestReadingTimedEvents:
    def test_maps_the_fields_the_tool_surface_promises(self):
        event = to_dict(TIMED)
        assert event["title"] == "Dentist"
        assert event["description"] == "bring the card"
        assert event["locations"] == ["Main Street Clinic"]
        assert event["start"] == "2026-08-27T10:00:00"
        assert event["duration"] == "PT1H"
        assert event["timeZone"] == "America/Chicago"
        assert event["isAllDay"] is False

    def test_start_is_reported_as_wall_time_not_utc(self):
        # The whole point of carrying timeZone separately.
        assert to_dict(TIMED)["start"].endswith("T10:00:00")

    def test_the_calendar_color_fills_in_when_the_event_has_none(self):
        assert to_dict(TIMED, calendar_color="#FF383C")["color"] == "#FF383C"


class TestReadingAllDayEvents:
    def test_a_date_valued_event_is_marked_all_day_and_has_no_zone(self):
        event = to_dict(ALL_DAY)
        assert event["isAllDay"] is True
        assert event["timeZone"] == ""
        assert event["duration"] == "P1D"
        assert event["start"] == "2026-08-24T00:00:00"

    def test_a_midnight_timed_event_is_not_all_day(self):
        # The distinction create_event's docstring warns about: a 24-hour block
        # starting at midnight is a timed event and shifts between zones.
        ics = TIMED.replace(
            "DTSTART;TZID=America/Chicago:20260827T100000",
            "DTSTART;TZID=America/Chicago:20260827T000000",
        ).replace(
            "DTEND;TZID=America/Chicago:20260827T110000",
            "DTEND;TZID=America/Chicago:20260828T000000",
        )
        event = to_dict(ics)
        assert event["isAllDay"] is False
        assert event["timeZone"] == "America/Chicago"


class TestRecurrenceIdentity:
    def test_the_key_keeps_the_literal_server_form(self):
        event = ical.parse_resource(OCCURRENCE)[0]
        assert ical.recurrence_key(event.get("RECURRENCE-ID")) == "20260902T143000Z"

    def test_the_displayed_recurrence_id_is_shown_in_the_events_own_zone(self):
        # iCloud sends RECURRENCE-ID in UTC. Reporting it raw put 14:30 next to
        # a start of 09:30, which reads as a different event.
        event = to_dict(OCCURRENCE)
        assert event["start"] == "2026-09-02T09:30:00"
        assert event["recurrenceId"] == "2026-09-02T09:30:00"

    def test_the_id_round_trips_through_encode_and_decode(self):
        event = to_dict(OCCURRENCE)
        calendar_id, name, key = ids.decode(event["id"])
        assert (calendar_id, name, key) == ("CAL", "event.ics", "20260902T143000Z")

    def test_a_key_becomes_a_value_in_the_masters_zone_not_relabeled(self):
        master_start = datetime(2026, 9, 1, 9, 30, tzinfo=CHICAGO)
        value = ical.recurrence_id_value("20260902T143000Z", master_start)
        # Same instant as 09:30 Chicago on the 2nd -- not 14:30 Chicago.
        assert value == datetime(2026, 9, 2, 9, 30, tzinfo=CHICAGO)

    def test_an_all_day_key_becomes_a_date(self):
        assert ical.recurrence_id_value("20260904", date(2026, 9, 1)) == date(2026, 9, 4)

    def test_a_master_has_no_recurrence_id(self):
        assert "recurrenceId" not in to_dict(RECURRING)


class TestParticipants:
    def test_attendees_carry_name_email_and_rsvp_status(self):
        people = to_dict(WITH_PEOPLE)["participants"]
        by_email = {person["email"]: person for person in people}
        assert by_email["sam@example.net"]["status"] == "accepted"
        assert by_email["jo@example.org"]["status"] == "needs-action"
        assert by_email["jo@example.org"]["name"] == "Jo"

    def test_the_organizer_is_flagged_as_owner(self):
        people = to_dict(WITH_PEOPLE)["participants"]
        owner = [person for person in people if person.get("roles", {}).get("owner")]
        assert [person["email"] for person in owner] == ["ada@example.com"]

    def test_an_event_with_nobody_reports_an_empty_list(self):
        assert to_dict(TIMED)["participants"] == []


class TestRecurrenceRules:
    def test_an_rrule_is_reported_in_the_shape_create_event_accepts(self):
        recurrence = to_dict(RECURRING)["recurrence"]
        assert recurrence == {
            "frequency": "weekly",
            "byDay": ["mo", "we", "fr"],
            "count": 6,
        }

    def test_the_reported_shape_is_accepted_back_without_translation(self):
        # Round-tripping matters: a model reads an event, edits one field, and
        # hands the whole thing back.
        parts = ical.recurrence_to_rrule(to_dict(RECURRING)["recurrence"])
        assert parts["FREQ"] == "WEEKLY"
        assert parts["BYDAY"] == ["MO", "WE", "FR"]
        assert parts["COUNT"] == 6

    def test_an_interval_of_one_is_left_implicit(self):
        assert "interval" not in (to_dict(RECURRING)["recurrence"])

    @pytest.mark.parametrize(
        ("recurrence", "fragment"),
        [
            ({}, "frequency"),
            ({"frequency": "fortnightly"}, "frequency"),
            ({"frequency": "weekly", "byDay": ["monday"]}, "byDay"),
            ({"frequency": "weekly", "interval": 0}, "at least 1"),
            ({"frequency": "weekly", "count": 3, "until": "2026-12-01"}, "not both"),
        ],
    )
    def test_bad_recurrence_says_which_field_is_wrong(self, recurrence, fragment):
        with pytest.raises(ical.ICalError) as excinfo:
            ical.recurrence_to_rrule(recurrence)
        assert fragment in str(excinfo.value)


class TestBuilding:
    def test_a_timed_event_carries_its_zone_and_a_matching_dtend(self):
        event = ical.build_event(
            uid="U1",
            title="Dentist",
            start=datetime(2026, 9, 3, 10, 0),
            duration=timedelta(hours=1),
            all_day=False,
            tzid="America/Chicago",
        )
        assert event["DTSTART"].params["TZID"] == "America/Chicago"
        assert event["DTEND"].dt - event["DTSTART"].dt == timedelta(hours=1)

    def test_an_all_day_event_is_date_valued_with_no_zone(self):
        event = ical.build_event(
            uid="U2",
            title="Offsite",
            start=date(2026, 9, 10),
            duration=timedelta(days=3),
            all_day=True,
            tzid="",
        )
        assert type(event["DTSTART"].dt) is date
        assert event["DTEND"].dt == date(2026, 9, 13)
        assert "TZID" not in event["DTSTART"].params

    def test_an_all_day_event_always_spans_at_least_one_day(self):
        event = ical.build_event(
            uid="U3",
            title="Holiday",
            start=date(2026, 9, 10),
            duration=timedelta(0),
            all_day=True,
            tzid="",
        )
        assert event["DTEND"].dt == date(2026, 9, 11)

    def test_a_resource_includes_a_vtimezone_for_every_zone_it_names(self):
        event = ical.build_event(
            uid="U4",
            title="Call",
            start=datetime(2026, 9, 3, 10, 0),
            duration=timedelta(hours=1),
            all_day=False,
            tzid="Europe/London",
        )
        body = ical.build_resource([event], {"Europe/London"})
        assert "BEGIN:VTIMEZONE" in body
        assert "TZID:Europe/London" in body

    def test_utc_needs_no_vtimezone(self):
        event = ical.build_event(
            uid="U5",
            title="Call",
            start=datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc),
            duration=timedelta(hours=1),
            all_day=False,
            tzid="UTC",
        )
        assert "BEGIN:VTIMEZONE" not in ical.build_resource([event], {"UTC"})

    def test_the_vtimezone_is_bounded_to_plausible_years(self):
        # Unbounded, icalendar emits every transition since 1883 on every write.
        body = ical.build_resource([], {"America/Chicago"})
        assert "1883" not in body
        assert len(body.splitlines()) < 60

    def test_retiming_drops_a_stale_duration(self):
        # DTEND and DURATION must never both be present; they can disagree.
        event = ical.parse_resource(TIMED)[0]
        event.add("DURATION", timedelta(hours=9))
        ical._set_timing(
            event,
            start=datetime(2026, 9, 3, 10, 0),
            duration=timedelta(minutes=30),
            all_day=False,
            tzid="America/Chicago",
        )
        assert "DURATION" not in event
        assert event["DTEND"].dt - event["DTSTART"].dt == timedelta(minutes=30)


class TestTouch:
    def test_bumping_advances_sequence_so_attendees_see_an_update(self):
        event = ical.parse_resource(TIMED)[0]
        event.add("SEQUENCE", 3)
        ical.touch(event)
        assert int(event["SEQUENCE"]) == 4

    def test_an_event_with_no_sequence_starts_at_one(self):
        event = ical.parse_resource(TIMED)[0]
        ical.touch(event)
        assert int(event["SEQUENCE"]) == 1


class TestParseResource:
    def test_the_master_comes_first_even_when_stored_second(self):
        merged = OCCURRENCE.replace("BEGIN:VCALENDAR\nVERSION:2.0\n", "").replace(
            "END:VCALENDAR\n", ""
        )
        combined = RECURRING.replace("END:VCALENDAR", merged + "END:VCALENDAR")
        events = ical.parse_resource(combined)
        assert len(events) == 2
        assert events[0].get("RECURRENCE-ID") is None

    def test_an_empty_resource_is_rejected_rather_than_returning_nothing(self):
        with pytest.raises(ical.ICalError):
            ical.parse_resource("BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n")


class TestAppleAddressRewriting:
    """iCloud does not leave a usable address in the property value.

    Stored events carry an internal principal URL; outbound iMIP mail carries an
    opaque `…@imip.me.com` reply-routing token. Both keep the real address only
    in the EMAIL parameter, so that is what must be read first.
    """

    def event_with(self, organizer_value):
        ics = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:apple-rewrite
SUMMARY:Book Club
DTSTART;TZID=America/Chicago:20260917T140000
DTEND;TZID=America/Chicago:20260917T143000
ORGANIZER;EMAIL=ada@example.com;CN=Ada:{organizer_value}
ATTENDEE;CUTYPE=INDIVIDUAL;PARTSTAT=ACCEPTED;EMAIL=ada@example.com:{organizer_value}
ATTENDEE;EMAIL=jo@example.org;RSVP=TRUE;PARTSTAT=NEEDS-ACTION:mailto:jo@example.org
END:VEVENT
END:VCALENDAR
"""
        return to_dict(ics)

    def test_the_stored_principal_url_form_still_yields_an_address(self):
        event = self.event_with("/aMTEwMjY5ODIxMTAyNjk4Mv8F-KLQtShy/principal/")
        owner = [p for p in event["participants"] if p.get("roles", {}).get("owner")]
        assert [p["email"] for p in owner] == ["ada@example.com"]

    def test_the_imip_token_form_still_yields_an_address(self):
        event = self.event_with("mailto:2_GEYTAMRWHE4DEMJRGAZDMOJYGL3ZWMGHH@imip.me.com")
        owner = [p for p in event["participants"] if p.get("roles", {}).get("owner")]
        assert [p["email"] for p in owner] == ["ada@example.com"]
        assert "imip.me.com" not in str(event["participants"])

    def test_a_plain_mailto_still_works_when_there_is_no_email_param(self):
        ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:plain
SUMMARY:Book Club
DTSTART;TZID=America/Chicago:20260917T140000
DTEND;TZID=America/Chicago:20260917T143000
ORGANIZER;CN=Ada:mailto:ada@example.com
END:VEVENT
END:VCALENDAR
"""
        people = to_dict(ics)["participants"]
        assert [p["email"] for p in people] == ["ada@example.com"]
