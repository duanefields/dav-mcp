"""Tool behavior, with the CalDAV layer mocked out.

Nothing here touches iCloud. What is being pinned is the part that sits between
the model and the protocol: argument validation, text matching, ordering, and
error messages that tell the model what to do differently.
"""

from unittest.mock import AsyncMock, patch

import pytest

from calendar_mcp import ids, server
from calendar_mcp.caldav import AuthError, Calendar, NotFound, Resource

PERSONAL = Calendar(
    id="CAL-PERSONAL",
    name="Personal",
    color="#FF383C",
    url="https://example.invalid/cal/CAL-PERSONAL/",
    components=("VEVENT",),
    read_only=False,
)

READ_ONLY = Calendar(
    id="CAL-SHARED",
    name="Shared",
    color="#00FF00",
    url="https://example.invalid/cal/CAL-SHARED/",
    components=("VEVENT",),
    read_only=True,
)


def event_ics(uid, summary, start="20260903T100000", location="", description=""):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        f"DTSTART;TZID=America/Chicago:{start}",
        f"DTEND;TZID=America/Chicago:{start[:9]}110000",
    ]
    if location:
        lines.append(f"LOCATION:{location}")
    if description:
        lines.append(f"DESCRIPTION:{description}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def resource(uid, summary, **kwargs):
    return Resource(
        calendar_id=PERSONAL.id,
        name=f"{uid}.ics",
        url=f"{PERSONAL.url}{uid}.ics",
        etag=f'"{uid}-etag"',
        ics=event_ics(uid, summary, **kwargs),
    )


@pytest.fixture
def caldav():
    """A stand-in CalDAV client wired into the server's lazy accessor."""
    fake = AsyncMock()
    fake.calendars = AsyncMock(return_value=[PERSONAL])
    fake.calendar = AsyncMock(return_value=PERSONAL)
    fake.default_calendar = AsyncMock(return_value=PERSONAL)
    fake.query = AsyncMock(return_value=[])
    fake.identities = AsyncMock(
        return_value=("mailto:me@example.com", "mailto:me@example.net")
    )
    fake.put = AsyncMock(return_value='"new-etag"')
    fake.delete = AsyncMock(return_value=None)
    with patch.object(server, "client", return_value=fake):
        yield fake


def text_of(result):
    return result.content[0].text


class TestListCalendars:
    async def test_lists_id_name_and_which_is_default(self, caldav):
        result = await server.list_calendars()
        items = result.structured_content["items"]
        assert items[0]["id"] == "CAL-PERSONAL"
        assert items[0]["isDefault"] is True
        assert "Personal (CAL-PERSONAL)" in text_of(result)

    async def test_a_read_only_calendar_is_never_the_default(self, caldav):
        caldav.calendars.return_value = [READ_ONLY, PERSONAL]
        items = (await server.list_calendars()).structured_content["items"]
        assert items[0]["readOnly"] is True
        assert items[0]["isDefault"] is False
        assert items[1]["isDefault"] is True

    async def test_bad_credentials_are_reported_with_what_to_fix(self, caldav):
        caldav.calendars.side_effect = AuthError("app-specific password required")
        result = await server.list_calendars()
        assert "error" in result.structured_content
        assert "app-specific password" in text_of(result)


class TestSearchEvents:
    async def test_returns_events_in_start_order(self, caldav):
        caldav.query.return_value = [
            resource("b", "Later", start="20260903T150000"),
            resource("a", "Earlier", start="20260903T090000"),
        ]
        items = (await server.search_events()).structured_content["items"]
        assert [item["title"] for item in items] == ["Earlier", "Later"]

    async def test_an_empty_range_says_so_rather_than_returning_nothing(self, caldav):
        result = await server.search_events(after="tomorrow", before="today")
        assert "error" in result.structured_content
        assert "range is empty" in text_of(result)

    @pytest.mark.parametrize("limit", [0, -1, 51, 999])
    async def test_out_of_bounds_limits_are_refused(self, caldav, limit):
        result = await server.search_events(limit=limit)
        assert "error" in result.structured_content

    async def test_the_limit_caps_results_but_total_reports_the_full_count(self, caldav):
        caldav.query.return_value = [
            resource(f"u{n}", f"Event {n}", start=f"2026090{n}T100000")
            for n in range(1, 6)
        ]
        structured = (await server.search_events(limit=2)).structured_content
        assert structured["count"] == 2
        assert structured["total"] == 5

    async def test_an_unparseable_event_does_not_sink_the_whole_search(self, caldav):
        broken = Resource(PERSONAL.id, "bad.ics", "url", '"e"', "not a calendar at all")
        caldav.query.return_value = [broken, resource("ok", "Fine")]
        items = (await server.search_events()).structured_content["items"]
        assert [item["title"] for item in items] == ["Fine"]

    async def test_an_unknown_calendar_id_names_the_way_out(self, caldav):
        result = await server.search_events(calendarId="nope")
        assert "list_calendars" in text_of(result)


class TestSearchTextMatching:
    """iCloud drops a text filter whenever a time range is present, so matching
    happens here. It covers the fields Fastmail's query covers."""

    @pytest.fixture
    def stocked(self, caldav):
        caldav.query.return_value = [
            resource("a", "Dentist appointment"),
            resource("b", "Standup", location="Zoom Room 4"),
            resource("c", "Lunch", description="with Priya about the budget"),
        ]
        return caldav

    async def test_matches_the_title(self, stocked):
        items = (await server.search_events(query="dentist")).structured_content["items"]
        assert [item["title"] for item in items] == ["Dentist appointment"]

    async def test_matching_is_case_insensitive(self, stocked):
        items = (await server.search_events(query="DENTIST")).structured_content["items"]
        assert len(items) == 1

    async def test_matches_the_location(self, stocked):
        items = (await server.search_events(query="zoom")).structured_content["items"]
        assert [item["title"] for item in items] == ["Standup"]

    async def test_matches_the_description(self, stocked):
        items = (await server.search_events(query="priya")).structured_content["items"]
        assert [item["title"] for item in items] == ["Lunch"]

    async def test_no_match_reports_empty_rather_than_erroring(self, stocked):
        result = await server.search_events(query="kayaking")
        assert result.structured_content["total"] == 0
        assert "No events found" in text_of(result)

    async def test_a_blank_query_is_treated_as_no_filter(self, stocked):
        assert (await server.search_events(query="   ")).structured_content["total"] == 3


class TestCreateEvent:
    async def test_writes_once_and_returns_a_usable_id(self, caldav):
        result = await server.create_event(title="Dentist", start="2026-09-03T10:00:00")
        caldav.put.assert_awaited_once()
        calendar_id, name, recurrence = ids.decode(result.structured_content["id"])
        assert calendar_id == PERSONAL.id
        assert name.endswith(".ics")
        assert recurrence == ""

    async def test_creates_with_if_none_match_so_a_collision_cannot_overwrite(self, caldav):
        await server.create_event(title="Dentist", start="2026-09-03T10:00:00")
        assert caldav.put.await_args.kwargs["create"] is True

    async def test_an_all_day_event_is_date_valued(self, caldav):
        await server.create_event(title="Offsite", start="2026-09-10", isAllDay=True, duration="P3D")
        body = caldav.put.await_args.args[2]
        assert "DTSTART;VALUE=DATE:20260910" in body
        assert "DTEND;VALUE=DATE:20260913" in body

    async def test_a_bare_start_is_read_as_wall_time_in_the_named_zone(self, caldav):
        await server.create_event(
            title="Call", start="2026-09-03T14:00:00", timeZone="Europe/London"
        )
        body = caldav.put.await_args.args[2]
        assert "DTSTART;TZID=Europe/London:20260903T140000" in body

    async def test_a_start_with_an_offset_keeps_its_instant(self, caldav):
        # 14:00Z is 15:00 in London during BST -- relabeling would move it.
        await server.create_event(
            title="Call", start="2026-09-03T14:00:00+00:00", timeZone="Europe/London"
        )
        body = caldav.put.await_args.args[2]
        assert "DTSTART;TZID=Europe/London:20260903T150000" in body

    async def test_an_unknown_zone_is_refused_before_anything_is_written(self, caldav):
        result = await server.create_event(
            title="Call", start="2026-09-03T10:00:00", timeZone="Mars/Olympus"
        )
        assert "not a known IANA time zone" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_a_read_only_calendar_is_refused_before_writing(self, caldav):
        caldav.calendar.return_value = READ_ONLY
        result = await server.create_event(
            title="Dentist", start="2026-09-03T10:00:00", calendarId=READ_ONLY.id
        )
        assert "read-only" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_an_empty_title_is_refused(self, caldav):
        result = await server.create_event(title="   ", start="2026-09-03T10:00:00")
        assert "title is required" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_a_bad_recurrence_is_refused_before_writing(self, caldav):
        result = await server.create_event(
            title="Standup",
            start="2026-09-03T10:00:00",
            recurrence={"frequency": "fortnightly"},
        )
        assert "frequency" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_a_failed_write_is_reported_as_a_failure_not_a_success(self, caldav):
        caldav.put.side_effect = NotFound("calendar vanished")
        result = await server.create_event(title="Dentist", start="2026-09-03T10:00:00")
        assert "error" in result.structured_content
        assert "Could not create" in text_of(result)


class TestUpdateEvent:
    async def test_writes_with_if_match_so_a_concurrent_edit_cannot_be_clobbered(self, caldav):
        caldav.get = AsyncMock(return_value=resource("u1", "Dentist"))
        await server.update_event(id=ids.encode(PERSONAL.id, "u1.ics"), title="Dentist v2")
        assert caldav.put.await_args.kwargs["etag"] == '"u1-etag"'

    async def test_only_the_named_fields_change(self, caldav):
        caldav.get = AsyncMock(
            return_value=resource("u1", "Dentist", location="Clinic", description="bring card")
        )
        await server.update_event(id=ids.encode(PERSONAL.id, "u1.ics"), title="Dentist v2")
        body = caldav.put.await_args.args[2]
        assert "SUMMARY:Dentist v2" in body
        assert "LOCATION:Clinic" in body
        assert "bring card" in body

    async def test_an_empty_string_clears_a_field(self, caldav):
        caldav.get = AsyncMock(return_value=resource("u1", "Dentist", location="Clinic"))
        await server.update_event(id=ids.encode(PERSONAL.id, "u1.ics"), location="")
        assert "LOCATION" not in caldav.put.await_args.args[2]

    async def test_a_hand_written_id_is_refused_with_advice(self, caldav):
        result = await server.update_event(id="event-42", title="x")
        assert "search_events" in text_of(result)

    async def test_recurrence_cannot_be_changed_from_an_occurrence_id(self, caldav):
        ics = event_ics("u1", "Standup").replace(
            "END:VEVENT", "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR\r\nEND:VEVENT"
        )
        caldav.get = AsyncMock(
            return_value=Resource(PERSONAL.id, "u1.ics", "url", '"e"', ics)
        )
        result = await server.update_event(
            id=ids.encode(PERSONAL.id, "u1.ics", "20260902T143000Z"),
            recurrence={"frequency": "daily"},
        )
        assert "whole series" in text_of(result)

    async def test_an_occurrence_id_on_a_non_recurring_event_is_explained(self, caldav):
        caldav.get = AsyncMock(return_value=resource("u1", "Dentist"))
        result = await server.update_event(
            id=ids.encode(PERSONAL.id, "u1.ics", "20260902T143000Z"), title="x"
        )
        assert "not recurring" in text_of(result)


class TestDeleteEvent:
    async def test_deleting_a_plain_event_removes_the_resource(self, caldav):
        result = await server.delete_event(id=ids.encode(PERSONAL.id, "u1.ics"))
        caldav.delete.assert_awaited_once()
        assert result.structured_content["deleted"] is True

    async def test_an_already_deleted_event_is_not_an_error(self, caldav):
        # The model retrying a delete should not be told something broke.
        caldav.delete.side_effect = NotFound("gone")
        result = await server.delete_event(id=ids.encode(PERSONAL.id, "u1.ics"))
        assert "error" not in result.structured_content
        assert result.structured_content["reason"] == "not-found"

    async def test_cancelling_an_occurrence_edits_the_series_instead_of_deleting_it(self, caldav):
        ics = event_ics("u1", "Standup").replace(
            "END:VEVENT", "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR\r\nEND:VEVENT"
        )
        caldav.get = AsyncMock(
            return_value=Resource(PERSONAL.id, "u1.ics", "url", '"e"', ics)
        )
        result = await server.delete_event(
            id=ids.encode(PERSONAL.id, "u1.ics", "20260904T150000Z")
        )
        caldav.delete.assert_not_awaited()
        assert "EXDATE" in caldav.put.await_args.args[2]
        assert "2026-09-04" in text_of(result)

    async def test_a_read_only_calendar_is_refused(self, caldav):
        caldav.calendar.return_value = READ_ONLY
        result = await server.delete_event(id=ids.encode(READ_ONLY.id, "u1.ics"))
        assert "read-only" in text_of(result)
        caldav.delete.assert_not_awaited()


class TestFormatting:
    def test_an_all_day_event_reads_as_a_day_not_a_midnight_time(self):
        text = server.format_event(
            {
                "title": "Offsite",
                "start": "2026-09-10T00:00:00",
                "duration": "P1D",
                "isAllDay": True,
                "id": "X",
            }
        )
        assert "2026-09-10 (all day)" in text
        assert "00:00:00" not in text

    def test_a_timed_event_shows_its_zone(self):
        text = server.format_event(
            {
                "title": "Call",
                "start": "2026-09-03T10:00:00",
                "duration": "PT1H",
                "timeZone": "America/Chicago",
                "isAllDay": False,
                "id": "X",
            }
        )
        assert "America/Chicago" in text and "PT1H" in text

    def test_the_id_is_always_present_so_a_follow_up_call_can_use_it(self):
        text = server.format_event({"title": "x", "start": "", "id": "ABC123"})
        assert "ID: ABC123" in text

    def test_a_long_description_is_truncated(self):
        text = server.format_event(
            {"title": "x", "start": "", "id": "X", "description": "word " * 500}
        )
        assert len(text) < 600
        assert "..." in text


# ----------------------------------------------------------------------
# Scheduling
#
# iCloud sends the iMIP mail itself, so every one of these paths puts real
# email in a real stranger's inbox. The assertion that matters most in this
# section is `put.assert_not_awaited()`: a rejected argument must send nothing
# at all, because a half-sent invitation cannot be recalled.
# ----------------------------------------------------------------------


def body_of(caldav):
    return caldav.put.await_args.args[2]


class TestCreateWithParticipants:
    async def test_invites_produce_an_organizer_and_attendees(self, caldav):
        await server.create_event(
            title="Book Club",
            start="2026-09-03T19:00:00",
            participants=[{"name": "Jo", "email": "jo@example.org"}],
        )
        body = body_of(caldav)
        assert "ORGANIZER" in body and "me@example.com" in body
        assert "ATTENDEE" in body and "jo@example.org" in body

    async def test_the_organizer_appears_on_their_own_guest_list_as_accepted(self, caldav):
        result = await server.create_event(
            title="Book Club",
            start="2026-09-03T19:00:00",
            participants=[{"name": "Jo", "email": "jo@example.org"}],
        )
        people = {p["email"]: p for p in result.structured_content["event"]["participants"]}
        assert people["me@example.com"]["status"] == "accepted"
        assert people["me@example.com"]["roles"] == {"owner": True}
        assert people["jo@example.org"]["status"] == "needs-action"

    async def test_it_names_the_organizer_and_the_invitee(self, caldav):
        result = await server.create_event(
            title="Book Club",
            start="2026-09-03T19:00:00",
            participants=[{"email": "jo@example.org"}],
        )
        assert "me@example.com" in text_of(result)
        assert result.structured_content["invited"] == ["jo@example.org"]

    async def test_an_event_with_no_participants_gets_no_organizer(self, caldav):
        # An ORGANIZER on a solo event makes it a meeting with one guest, and
        # some clients then mail on every edit.
        await server.create_event(title="Dentist", start="2026-09-03T10:00:00")
        assert "ORGANIZER" not in body_of(caldav)

    async def test_from_may_be_any_of_the_accounts_own_addresses(self, caldav):
        await server.create_event(
            title="Book Club",
            start="2026-09-03T19:00:00",
            participants=[{"email": "jo@example.org"}],
            organizer="me@example.net",
        )
        assert "me@example.net" in body_of(caldav)

    async def test_a_foreign_from_is_refused_before_any_mail_goes_out(self, caldav):
        # iCloud accepts the PUT and silently sends nothing, which is
        # indistinguishable from success -- so this has to be caught here.
        result = await server.create_event(
            title="Book Club",
            start="2026-09-03T19:00:00",
            participants=[{"email": "jo@example.org"}],
            organizer="someone.else@example.com",
        )
        assert "not one of this account's addresses" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_from_without_participants_is_refused(self, caldav):
        result = await server.create_event(
            title="Dentist", start="2026-09-03T10:00:00", organizer="me@example.com"
        )
        assert "only applies when inviting participants" in text_of(result)
        caldav.put.assert_not_awaited()

    @pytest.mark.parametrize(
        "participants",
        [
            "jo@example.org",
            [{"name": "Jo"}],
            [{"email": ""}],
            [{"email": "not-an-address"}],
            ["jo@example.org"],
        ],
    )
    async def test_malformed_participants_send_nothing_at_all(self, caldav, participants):
        result = await server.create_event(
            title="Book Club", start="2026-09-03T19:00:00", participants=participants
        )
        assert "error" in result.structured_content
        caldav.put.assert_not_awaited()

    async def test_the_same_person_twice_is_invited_once(self, caldav):
        await server.create_event(
            title="Book Club",
            start="2026-09-03T19:00:00",
            participants=[
                {"email": "jo@example.org"},
                {"email": "JO@example.org", "name": "Jo again"},
            ],
        )
        assert body_of(caldav).lower().count("jo@example.org") == 2  # ATTENDEE + EMAIL param


def meeting_ics(uid="u1", partstat="NEEDS-ACTION"):
    return "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "SUMMARY:Book Club",
        "DTSTART;TZID=America/Chicago:20260903T190000",
        "DTEND;TZID=America/Chicago:20260903T200000",
        "ORGANIZER;CN=Ada;EMAIL=ada@example.com:mailto:ada@example.com",
        "ATTENDEE;CN=Ada;PARTSTAT=ACCEPTED;EMAIL=ada@example.com:mailto:ada@example.com",
        f"ATTENDEE;CN=Me;PARTSTAT={partstat};EMAIL=me@example.com:mailto:me@example.com",
        "ATTENDEE;CN=Jo;PARTSTAT=NEEDS-ACTION;EMAIL=jo@example.org:mailto:jo@example.org",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])


def meeting(caldav, uid="u1", partstat="NEEDS-ACTION"):
    caldav.get = AsyncMock(
        return_value=Resource(PERSONAL.id, f"{uid}.ics", "url", '"e"', meeting_ics(uid, partstat))
    )
    return ids.encode(PERSONAL.id, f"{uid}.ics")


class TestUpdateParticipants:
    async def test_adding_an_invitee_records_who_was_added(self, caldav):
        result = await server.update_event(
            id=meeting(caldav), addParticipants=[{"name": "Sam", "email": "sam@example.net"}]
        )
        assert "sam@example.net" in body_of(caldav)
        assert result.structured_content["invited"] == ["sam@example.net"]

    async def test_someone_already_invited_is_not_invited_twice(self, caldav):
        result = await server.update_event(
            id=meeting(caldav), addParticipants=[{"email": "jo@example.org"}]
        )
        assert result.structured_content["invited"] == []

    async def test_removing_by_email_uninvites_exactly_that_person(self, caldav):
        result = await server.update_event(
            id=meeting(caldav), removeParticipants=["jo@example.org"]
        )
        assert "jo@example.org" not in body_of(caldav)
        assert "me@example.com" in body_of(caldav)
        assert result.structured_content["uninvited"] == ["jo@example.org"]

    async def test_removing_by_display_name_works(self, caldav):
        await server.update_event(id=meeting(caldav), removeParticipants=["Jo"])
        assert "jo@example.org" not in body_of(caldav)

    async def test_the_organizer_is_never_removed(self, caldav):
        # Dropping the organizer orphans the event for every other guest.
        result = await server.update_event(
            id=meeting(caldav), removeParticipants=["ada@example.com"]
        )
        assert "ada@example.com" in body_of(caldav)
        assert result.structured_content["uninvited"] == []

    async def test_an_unknown_name_is_ignored_rather_than_failing_the_edit(self, caldav):
        result = await server.update_event(
            id=meeting(caldav), removeParticipants=["Nobody At All"]
        )
        assert "error" not in result.structured_content

    async def test_an_ambiguous_name_changes_nothing(self, caldav):
        ics = meeting_ics().replace(
            "ATTENDEE;CN=Jo;PARTSTAT=NEEDS-ACTION;EMAIL=jo@example.org:mailto:jo@example.org",
            "ATTENDEE;CN=Jo;PARTSTAT=NEEDS-ACTION;EMAIL=jo@example.org:mailto:jo@example.org\r\n"
            "ATTENDEE;CN=Jo;PARTSTAT=NEEDS-ACTION;EMAIL=jo2@example.org:mailto:jo2@example.org",
        )
        caldav.get = AsyncMock(return_value=Resource(PERSONAL.id, "u1.ics", "url", '"e"', ics))
        result = await server.update_event(
            id=ids.encode(PERSONAL.id, "u1.ics"), removeParticipants=["Jo"]
        )
        assert "Pass the email address" in text_of(result)
        assert "Nothing was changed" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_participants_cannot_be_changed_on_a_single_occurrence(self, caldav):
        ics = meeting_ics().replace("END:VEVENT", "RRULE:FREQ=WEEKLY\r\nEND:VEVENT")
        caldav.get = AsyncMock(return_value=Resource(PERSONAL.id, "u1.ics", "url", '"e"', ics))
        result = await server.update_event(
            id=ids.encode(PERSONAL.id, "u1.ics", "20260910T000000Z"),
            addParticipants=[{"email": "sam@example.net"}],
        )
        assert "whole series" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_editing_an_event_with_guests_warns_that_mail_goes_out(self, caldav):
        # A one-word title fix still mails everyone. Saying so is the point.
        result = await server.update_event(id=meeting(caldav), title="Book Club (moved)")
        assert "mailed them the update" in text_of(result)

    async def test_editing_an_event_with_no_guests_makes_no_such_claim(self, caldav):
        caldav.get = AsyncMock(return_value=resource("u1", "Dentist"))
        result = await server.update_event(id=ids.encode(PERSONAL.id, "u1.ics"), title="Dentist v2")
        assert "mailed" not in text_of(result)


class TestRsvp:
    async def test_accepting_sets_partstat_on_our_own_attendee_line(self, caldav):
        result = await server.rsvp_event(id=meeting(caldav), status="accepted")
        body = body_of(caldav)
        mine = [l for l in body.splitlines() if "me@example.com" in l and "ATTENDEE" in l]
        assert mine and "PARTSTAT=ACCEPTED" in mine[0]
        assert result.structured_content["status"] == "accepted"
        assert result.structured_content["respondedAs"] == "me@example.com"

    async def test_answering_stops_the_server_asking_again(self, caldav):
        await server.rsvp_event(id=meeting(caldav), status="declined")
        mine = [
            l for l in body_of(caldav).splitlines()
            if "me@example.com" in l and "ATTENDEE" in l
        ]
        assert "RSVP=FALSE" in mine[0]

    async def test_nobody_elses_status_is_touched(self, caldav):
        await server.rsvp_event(id=meeting(caldav), status="declined")
        body = body_of(caldav)
        jo = [l for l in body.splitlines() if "jo@example.org" in l and "ATTENDEE" in l]
        assert "PARTSTAT=NEEDS-ACTION" in jo[0]

    async def test_it_says_the_organizer_was_told(self, caldav):
        result = await server.rsvp_event(id=meeting(caldav), status="tentative")
        assert "replied to ada@example.com" in text_of(result)

    @pytest.mark.parametrize("status", ["yes", "maybe", "", "ACCEPT", None])
    async def test_an_unsupported_status_is_refused(self, caldav, status):
        result = await server.rsvp_event(id=meeting(caldav), status=status)
        assert "accepted, tentative, declined" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_rsvping_to_your_own_event_is_refused_with_the_alternative(self, caldav):
        ics = meeting_ics().replace("ada@example.com", "me@example.com")
        caldav.get = AsyncMock(return_value=Resource(PERSONAL.id, "u1.ics", "url", '"e"', ics))
        result = await server.rsvp_event(id=ids.encode(PERSONAL.id, "u1.ics"), status="accepted")
        assert "organizer of this event" in text_of(result)
        assert "update_event" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_rsvping_when_not_invited_lists_who_is(self, caldav):
        caldav.identities = AsyncMock(return_value=("mailto:stranger@example.com",))
        result = await server.rsvp_event(id=meeting(caldav), status="accepted")
        assert "not on this event's guest list" in text_of(result)
        assert "jo@example.org" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_an_event_with_nobody_has_nothing_to_answer(self, caldav):
        caldav.get = AsyncMock(return_value=resource("u1", "Dentist"))
        result = await server.rsvp_event(id=ids.encode(PERSONAL.id, "u1.ics"), status="accepted")
        assert "nothing to respond to" in text_of(result)
        caldav.put.assert_not_awaited()

    async def test_any_of_our_addresses_can_be_the_invited_one(self, caldav):
        # The invitation names one address; the account owns several.
        ics = meeting_ics().replace("me@example.com", "me@example.net")
        caldav.get = AsyncMock(return_value=Resource(PERSONAL.id, "u1.ics", "url", '"e"', ics))
        result = await server.rsvp_event(id=ids.encode(PERSONAL.id, "u1.ics"), status="accepted")
        assert result.structured_content["respondedAs"] == "me@example.net"


class TestDeliveryReporting:
    """iCloud stamps SCHEDULE-STATUS on each attendee after trying to deliver.

    Reading it back is what separates "we sent it" from "we asked iCloud to
    send it and it bounced". Claiming the former when the latter happened is
    worse than saying nothing: the user stops chasing an invitation that never
    arrived. Observed live: 1.1 to a real mailbox, 5.1 to one that refused.
    """

    def scheduled(self, caldav, status):
        """Make the read-back return an attendee carrying `status`."""
        line = "ATTENDEE;CN=Jo;PARTSTAT=NEEDS-ACTION;EMAIL=jo@example.org"
        if status:
            line += f";SCHEDULE-STATUS={status}"
        ics = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:u1",
            "SUMMARY:Book Club",
            "DTSTART;TZID=America/Chicago:20260903T190000",
            "DTEND;TZID=America/Chicago:20260903T200000",
            "ORGANIZER;EMAIL=me@example.com:mailto:me@example.com",
            f"{line}:mailto:jo@example.org",
            "END:VEVENT", "END:VCALENDAR", "",
        ])
        caldav.get = AsyncMock(
            return_value=Resource(PERSONAL.id, "u1.ics", "url", '"e"', ics)
        )

    async def test_a_delivered_invitation_is_reported_as_delivered(self, caldav):
        self.scheduled(caldav, "1.1")
        result = await server.create_event(
            title="Book Club", start="2026-09-03T19:00:00",
            participants=[{"email": "jo@example.org"}],
        )
        assert "delivered invitations to jo@example.org" in text_of(result)
        assert result.structured_content["undelivered"] == []

    async def test_a_failed_delivery_is_reported_as_a_failure(self, caldav):
        # The live case: maildrop.cc refused the message and iCloud said 5.1,
        # while the tool cheerfully claimed the invitation had been sent.
        self.scheduled(caldav, "5.1")
        result = await server.create_event(
            title="Book Club", start="2026-09-03T19:00:00",
            participants=[{"email": "jo@example.org"}],
        )
        assert "COULD NOT deliver" in text_of(result)
        assert "have not been told" in text_of(result)
        assert result.structured_content["undelivered"] == ["jo@example.org (5.1)"]

    async def test_an_invalid_calendar_user_is_also_a_failure(self, caldav):
        self.scheduled(caldav, "3.7")
        result = await server.create_event(
            title="Book Club", start="2026-09-03T19:00:00",
            participants=[{"email": "jo@example.org"}],
        )
        assert result.structured_content["undelivered"] == ["jo@example.org (3.7)"]

    async def test_no_status_yet_is_reported_as_pending_not_as_success(self, caldav):
        self.scheduled(caldav, "")
        result = await server.create_event(
            title="Book Club", start="2026-09-03T19:00:00",
            participants=[{"email": "jo@example.org"}],
        )
        assert "pending" in text_of(result)
        assert result.structured_content["undelivered"] == []

    async def test_a_read_back_failure_does_not_fail_the_write(self, caldav):
        # The event exists either way; losing the receipt must not report the
        # create as broken.
        caldav.get = AsyncMock(side_effect=NotFound("gone"))
        result = await server.create_event(
            title="Book Club", start="2026-09-03T19:00:00",
            participants=[{"email": "jo@example.org"}],
        )
        assert "error" not in result.structured_content
        assert result.structured_content["id"]

    async def test_no_extra_read_when_there_are_no_participants(self, caldav):
        caldav.get = AsyncMock()
        await server.create_event(title="Dentist", start="2026-09-03T10:00:00")
        caldav.get.assert_not_awaited()


# ----------------------------------------------------------------------
# find_free_time
#
# The interval maths lives in test_availability.py. What is pinned here is the
# policy: which events count as busy at all. Get that wrong and the tool fails
# in opposite directions -- count everything and a calendar with a birthday on
# it has no free time; count nothing and it offers slots during real meetings.
# ----------------------------------------------------------------------


def timed(uid, summary, day, hour, hours=1, extra=()):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT",
        f"UID:{uid}", f"SUMMARY:{summary}",
        f"DTSTART;TZID=America/Chicago:202609{day:02d}T{hour:02d}0000",
        f"DTEND;TZID=America/Chicago:202609{day:02d}T{hour + hours:02d}0000",
        *extra, "END:VEVENT", "END:VCALENDAR", "",
    ]
    return Resource(PERSONAL.id, f"{uid}.ics", "url", '"e"', "\r\n".join(lines))


def all_day(uid, summary, day):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT",
        f"UID:{uid}", f"SUMMARY:{summary}",
        f"DTSTART;VALUE=DATE:202609{day:02d}",
        f"DTEND;VALUE=DATE:202609{day + 1:02d}",
        "END:VEVENT", "END:VCALENDAR", "",
    ]
    return Resource(PERSONAL.id, f"{uid}.ics", "url", '"e"', "\r\n".join(lines))


async def free_time(caldav, **kwargs):
    kwargs.setdefault("duration", "PT1H")
    kwargs.setdefault("after", "2026-09-14T00:00:00")
    kwargs.setdefault("before", "2026-09-15T00:00:00")
    return await server.find_free_time(**kwargs)


class TestFindFreeTime:
    async def test_an_empty_day_offers_the_whole_working_day(self, caldav):
        result = await free_time(caldav)
        items = result.structured_content["items"]
        assert items[0]["start"] == "2026-09-14T09:00:00"
        assert items[0]["end"] == "2026-09-14T17:00:00"

    async def test_a_meeting_splits_the_day(self, caldav):
        caldav.query.return_value = [timed("m1", "Standup", 14, 12)]
        items = (await free_time(caldav)).structured_content["items"]
        assert [(i["start"][11:16], i["end"][11:16]) for i in items] == [
            ("09:00", "12:00"),
            ("13:00", "17:00"),
        ]

    async def test_working_hours_are_configurable(self, caldav):
        items = (await free_time(caldav, dayStart="08:00", dayEnd="10:00")).structured_content["items"]
        assert items[0]["start"][11:16] == "08:00"
        assert items[0]["end"][11:16] == "10:00"

    async def test_a_backwards_working_day_is_refused(self, caldav):
        result = await free_time(caldav, dayStart="17:00", dayEnd="09:00")
        assert "must be later than" in text_of(result)

    async def test_a_malformed_clock_says_what_it_wanted(self, caldav):
        result = await free_time(caldav, dayStart="half nine")
        assert '"09:00"' in text_of(result)

    async def test_weekends_are_excluded_by_default(self, caldav):
        # 2026-09-19 is a Saturday.
        result = await free_time(
            caldav, after="2026-09-19T00:00:00", before="2026-09-20T23:59:00"
        )
        assert result.structured_content["items"] == []
        assert "weekends are excluded" in text_of(result)

    async def test_weekends_can_be_included(self, caldav):
        result = await free_time(
            caldav, after="2026-09-19T00:00:00", before="2026-09-20T23:59:00",
            includeWeekends=True,
        )
        assert result.structured_content["items"]

    async def test_a_zero_duration_is_refused(self, caldav):
        assert "positive" in text_of(await free_time(caldav, duration="PT0S"))

    async def test_an_empty_range_is_refused(self, caldav):
        result = await free_time(caldav, after="2026-09-15", before="2026-09-14")
        assert "range is empty" in text_of(result)

    async def test_no_opening_explains_how_to_widen_the_search(self, caldav):
        caldav.query.return_value = [timed("m1", "All day meeting", 14, 9, hours=8)]
        result = await free_time(caldav)
        assert result.structured_content["items"] == []
        assert "Widen the range" in text_of(result)

    async def test_the_limit_is_honored(self, caldav):
        result = await free_time(
            caldav, after="2026-09-14", before="2026-09-19", limit=2
        )
        assert result.structured_content["count"] == 2


class TestWhatCountsAsBusy:
    async def test_a_normal_meeting_blocks(self, caldav):
        caldav.query.return_value = [timed("m1", "Standup", 14, 12)]
        items = (await free_time(caldav)).structured_content["items"]
        assert len(items) == 2

    async def test_an_event_marked_free_does_not_block(self, caldav):
        # TRANSP:TRANSPARENT is the standard "on my calendar but not busy".
        caldav.query.return_value = [
            timed("m1", "Reminder", 14, 12, extra=["TRANSP:TRANSPARENT"])
        ]
        assert len((await free_time(caldav)).structured_content["items"]) == 1

    async def test_a_cancelled_event_does_not_block(self, caldav):
        caldav.query.return_value = [
            timed("m1", "Cancelled thing", 14, 12, extra=["STATUS:CANCELLED"])
        ]
        assert len((await free_time(caldav)).structured_content["items"]) == 1

    async def test_a_declined_invitation_does_not_block(self, caldav):
        # Apple leaves declined invitations on the calendar. Counting them
        # would block the week with meetings the user refused.
        caldav.query.return_value = [
            timed("m1", "Optional sync", 14, 12, extra=[
                "ORGANIZER;EMAIL=ada@example.com:mailto:ada@example.com",
                "ATTENDEE;PARTSTAT=DECLINED;EMAIL=me@example.com:mailto:me@example.com",
            ])
        ]
        assert len((await free_time(caldav)).structured_content["items"]) == 1

    async def test_an_accepted_invitation_still_blocks(self, caldav):
        caldav.query.return_value = [
            timed("m1", "Real meeting", 14, 12, extra=[
                "ORGANIZER;EMAIL=ada@example.com:mailto:ada@example.com",
                "ATTENDEE;PARTSTAT=ACCEPTED;EMAIL=me@example.com:mailto:me@example.com",
            ])
        ]
        assert len((await free_time(caldav)).structured_content["items"]) == 2

    async def test_an_unanswered_invitation_still_blocks(self, caldav):
        # Not having replied yet is not the same as having declined.
        caldav.query.return_value = [
            timed("m1", "Pending", 14, 12, extra=[
                "ORGANIZER;EMAIL=ada@example.com:mailto:ada@example.com",
                "ATTENDEE;PARTSTAT=NEEDS-ACTION;EMAIL=me@example.com:mailto:me@example.com",
            ])
        ]
        assert len((await free_time(caldav)).structured_content["items"]) == 2

    async def test_someone_elses_decline_does_not_free_our_time(self, caldav):
        caldav.query.return_value = [
            timed("m1", "Meeting", 14, 12, extra=[
                "ORGANIZER;EMAIL=me@example.com:mailto:me@example.com",
                "ATTENDEE;PARTSTAT=DECLINED;EMAIL=jo@example.org:mailto:jo@example.org",
            ])
        ]
        assert len((await free_time(caldav)).structured_content["items"]) == 2

    async def test_an_all_day_event_does_not_block_but_is_reported(self, caldav):
        # "First Day of School" does not occupy the day; a trip does. Rather
        # than guess, it is excluded and surfaced for the caller to weigh.
        caldav.query.return_value = [all_day("a1", "First Day of School", 14)]
        result = await free_time(caldav)
        assert len(result.structured_content["items"]) == 1
        assert result.structured_content["allDayEvents"][0]["title"] == "First Day of School"
        assert "not treated as busy" in text_of(result)
        assert "First Day of School" in text_of(result)


# ----------------------------------------------------------------------
# Contacts
# ----------------------------------------------------------------------

from calendar_mcp.carddav import AddressBook, Card
from calendar_mcp.carddav import NotFound as CardNotFound

BOOK = AddressBook(id="card", name="Contacts", url="https://example.invalid/card/", read_only=False)
BOOK_RO = AddressBook(id="ro", name="Shared", url="https://example.invalid/ro/", read_only=True)

CONTACT = """BEGIN:VCARD\r
VERSION:3.0\r
N:Collier;Andrew;;;\r
FN:Andrew Collier\r
EMAIL;type=INTERNET;type=HOME;type=pref:andrew@example.com\r
item1.EMAIL;type=INTERNET:a.collier@work.example\r
item1.X-ABLabel:_$!<Work>!$_\r
TEL;type=CELL;type=pref:(555) 010-0100\r
ORG:Acme;\r
X-SOCIALPROFILE;type=linkedin:https://linkedin.example/in/ac\r
UID:CONTACT-1\r
END:VCARD\r
"""


def card(uid="CONTACT-1", body=CONTACT):
    return Card(book_id=BOOK.id, name=f"{uid}.vcf", url=f"{BOOK.url}{uid}.vcf",
                etag='"c-etag"', vcard=body)


@pytest.fixture
def cards():
    fake = AsyncMock()
    fake.address_books = AsyncMock(return_value=[BOOK])
    fake.address_book = AsyncMock(return_value=BOOK)
    fake.default_book = AsyncMock(return_value=BOOK)
    fake.search = AsyncMock(return_value=[card()])
    fake.get = AsyncMock(return_value=card())
    fake.put = AsyncMock(return_value='"new"')
    fake.delete = AsyncMock(return_value=None)
    with patch.object(server, "contacts", return_value=fake):
        yield fake


def contact_id(uid="CONTACT-1"):
    return ids.encode(BOOK.id, f"{uid}.vcf")


class TestSearchContacts:
    async def test_returns_the_contact_with_every_way_to_reach_them(self, cards):
        result = await server.search_contacts(query="Collier")
        item = result.structured_content["items"][0]
        assert item["name"] == "Andrew Collier"
        assert item["emails"] == ["andrew@example.com", "a.collier@work.example"]
        assert item["phones"] == ["(555) 010-0100"]
        assert item["organization"] == "Acme"

    async def test_the_query_reaches_the_server(self, cards):
        # Server-side filtering matters: the real book has 900+ cards.
        await server.search_contacts(query="Collier")
        assert cards.search.await_args.args[1] == "Collier"

    async def test_an_omitted_query_lists_everything(self, cards):
        await server.search_contacts()
        assert cards.search.await_args.args[1] is None

    async def test_labels_are_rendered_in_the_text_channel(self, cards):
        assert "(Work)" in text_of(await server.search_contacts(query="Collier"))

    async def test_no_match_says_so_rather_than_erroring(self, cards):
        cards.search.return_value = []
        result = await server.search_contacts(query="nobody")
        assert "error" not in result.structured_content
        assert "No contacts matching" in text_of(result)

    async def test_an_unreadable_card_does_not_sink_the_search(self, cards):
        cards.search.return_value = [card("BAD", "not a vcard at all"), card()]
        items = (await server.search_contacts(query="x")).structured_content["items"]
        assert [i["name"] for i in items] == ["Andrew Collier"]

    @pytest.mark.parametrize("limit", [0, -1, 51])
    async def test_out_of_bounds_limits_are_refused(self, cards, limit):
        assert "error" in (await server.search_contacts(limit=limit)).structured_content

    async def test_an_unknown_address_book_names_the_way_out(self, cards):
        result = await server.search_contacts(addressBookId="nope")
        assert "list_address_books" in text_of(result)


class TestCreateContact:
    async def test_writes_once_and_returns_a_usable_id(self, cards):
        result = await server.create_contact(name="Jane Doe", emails=["jane@example.com"])
        cards.put.assert_awaited_once()
        book_id, resource, _ = ids.decode(result.structured_content["id"], kind="contact")
        assert book_id == BOOK.id and resource.endswith(".vcf")

    async def test_creates_with_if_none_match(self, cards):
        await server.create_contact(name="Jane Doe")
        assert cards.put.await_args.kwargs["create"] is True

    async def test_the_first_email_becomes_preferred(self, cards):
        await server.create_contact(
            name="Jane Doe", emails=["first@example.com", "second@example.com"]
        )
        body = cards.put.await_args.args[2]
        assert "pref" in body.split("first@example.com")[0].splitlines()[-1]

    async def test_an_empty_name_is_refused(self, cards):
        assert "name is required" in text_of(await server.create_contact(name="  "))
        cards.put.assert_not_awaited()

    async def test_something_that_is_not_an_email_is_refused(self, cards):
        result = await server.create_contact(name="Jane", emails=["not-an-address"])
        assert "do not look like email addresses" in text_of(result)
        cards.put.assert_not_awaited()

    async def test_a_read_only_book_is_refused_before_writing(self, cards):
        cards.address_book.return_value = BOOK_RO
        result = await server.create_contact(name="Jane", addressBookId=BOOK_RO.id)
        assert "read-only" in text_of(result)
        cards.put.assert_not_awaited()


class TestUpdateContact:
    async def test_writes_with_if_match(self, cards):
        await server.update_contact(id=contact_id(), name="Andrew C.")
        assert cards.put.await_args.kwargs["etag"] == '"c-etag"'

    async def test_unmodelled_properties_survive(self, cards):
        # The whole reason updates mutate rather than rebuild.
        await server.update_contact(id=contact_id(), organization="New Co")
        body = cards.put.await_args.args[2]
        assert "X-SOCIALPROFILE" in body
        assert "linkedin.example/in/ac" in body

    async def test_email_deltas_are_reported(self, cards):
        result = await server.update_contact(
            id=contact_id(),
            addEmails=["new@example.com"],
            removeEmails=["ANDREW@EXAMPLE.COM"],
        )
        sc = result.structured_content
        assert sc["addedEmails"] == ["new@example.com"]
        assert sc["removedEmails"] == ["andrew@example.com"]

    async def test_an_empty_string_clears_a_field(self, cards):
        await server.update_contact(id=contact_id(), organization="")
        assert "ORG" not in cards.put.await_args.args[2]

    async def test_an_empty_name_is_refused(self, cards):
        assert "cannot be empty" in text_of(
            await server.update_contact(id=contact_id(), name="   ")
        )

    async def test_a_hand_written_id_points_at_the_contact_tool(self, cards):
        result = await server.update_contact(id="contact-42", name="x")
        assert "search_contacts" in text_of(result)
        assert "search_events" not in text_of(result)


class TestDeleteContact:
    async def test_deleting_removes_the_card(self, cards):
        result = await server.delete_contact(id=contact_id())
        cards.delete.assert_awaited_once()
        assert result.structured_content["deleted"] is True

    async def test_an_already_deleted_contact_is_not_an_error(self, cards):
        cards.delete.side_effect = CardNotFound("gone")
        result = await server.delete_contact(id=contact_id())
        assert "error" not in result.structured_content
        assert result.structured_content["reason"] == "not-found"

    async def test_a_read_only_book_is_refused(self, cards):
        cards.address_book.return_value = BOOK_RO
        result = await server.delete_contact(id=ids.encode(BOOK_RO.id, "x.vcf"))
        assert "read-only" in text_of(result)
        cards.delete.assert_not_awaited()


class TestListAddressBooks:
    async def test_lists_id_and_name(self, cards):
        result = await server.list_address_books()
        assert result.structured_content["items"][0]["id"] == "card"
        assert "Contacts (card)" in text_of(result)
