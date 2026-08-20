"""Event id encoding.

Ids are the one thing that crosses the model boundary and has to come back
intact. They are also the thing a model is most likely to try to construct by
hand, so the failure message matters as much as the encoding.
"""

import pytest

from dav_mcp import ids


class TestRoundTrip:
    def test_a_plain_event_round_trips(self):
        encoded = ids.encode("CAL-1", "event.ics")
        assert ids.decode(encoded) == ("CAL-1", "event.ics", "")

    def test_an_occurrence_round_trips(self):
        encoded = ids.encode("CAL-1", "event.ics", "20260902T143000Z")
        assert ids.decode(encoded) == ("CAL-1", "event.ics", "20260902T143000Z")

    def test_a_resource_named_after_a_google_uid_survives(self):
        # Anything imported from Google is named "<uid>@google.com.ics", and the
        # "@" is also the occurrence separator -- so it has to split from the
        # right, not the left.
        name = "abc123def456ghi789@google.com.ics"
        assert ids.decode(ids.encode("CAL-1", name)) == ("CAL-1", name, "")

    def test_a_google_named_resource_with_an_occurrence_still_splits_correctly(self):
        name = "abc123def456ghi789@google.com.ics"
        encoded = ids.encode("CAL-1", name, "20260825T004500Z")
        assert ids.decode(encoded) == ("CAL-1", name, "20260825T004500Z")

    def test_the_encoding_is_url_and_json_safe(self):
        encoded = ids.encode("CAL/1", "a b+c.ics", "20260902T143000Z")
        assert all(char.isalnum() or char in "-_" for char in encoded)

    def test_ids_are_stable_across_calls(self):
        assert ids.encode("CAL", "e.ics") == ids.encode("CAL", "e.ics")

    def test_different_occurrences_get_different_ids(self):
        first = ids.encode("CAL", "e.ics", "20260902T143000Z")
        second = ids.encode("CAL", "e.ics", "20260904T143000Z")
        assert first != second


class TestRejection:
    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_an_empty_id_is_refused(self, value):
        with pytest.raises(ids.BadEventId):
            ids.decode(value)

    def test_a_hand_written_id_is_refused_with_advice(self):
        with pytest.raises(ids.BadEventId) as excinfo:
            ids.decode("event-42")
        assert "search_events" in str(excinfo.value)

    def test_valid_base64_that_is_not_one_of_ours_is_refused(self):
        import base64

        payload = base64.urlsafe_b64encode(b"no-separator-here").decode().rstrip("=")
        with pytest.raises(ids.BadEventId):
            ids.decode(payload)

    def test_encoding_requires_both_halves(self):
        with pytest.raises(ValueError):
            ids.encode("", "event.ics")
        with pytest.raises(ValueError):
            ids.encode("CAL", "")
