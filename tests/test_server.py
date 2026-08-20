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

    async def test_the_reply_says_mail_went_out_rather_than_implying_silence(self, caldav):
        result = await server.create_event(
            title="Book Club",
            start="2026-09-03T19:00:00",
            participants=[{"email": "jo@example.org"}],
        )
        assert "sent invitations" in text_of(result)
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
    async def test_adding_an_invitee_reports_who_was_mailed(self, caldav):
        result = await server.update_event(
            id=meeting(caldav), addParticipants=[{"name": "Sam", "email": "sam@example.net"}]
        )
        assert "sam@example.net" in body_of(caldav)
        assert result.structured_content["invited"] == ["sam@example.net"]
        assert "Invitations sent to" in text_of(result)

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
