"""Reading and writing vCards.

The fixtures reproduce the exact shapes iCloud returns, with identities
replaced. Apple's cards carry a great deal that this tool surface does not
model, and the tests that matter most here are the ones proving that material
survives an edit.
"""

import pytest

from dav_mcp import ids, vcard

# A card in the shape Apple actually writes: item-grouped labels, repeated
# type= params, an escaped multi-line street, social profiles, related names,
# a photo, and internal bookkeeping.
RICH = """BEGIN:VCARD\r
VERSION:3.0\r
PRODID:-//Apple Inc.//iOS 26.5.2//EN\r
N:Doe;Jane;;;\r
FN:Jane Doe\r
EMAIL;type=INTERNET;type=HOME;type=pref:jane@example.com\r
item2.EMAIL;type=INTERNET:jane.doe@work.example\r
item2.X-ABLabel:_$!<Work>!$_\r
TEL;type=CELL;type=VOICE;type=pref:(555) 010-0100\r
item3.TEL:(555) 010-0200\r
item3.X-ABLabel:Google Voice\r
item4.ADR;type=HOME;type=pref:;;100 Main Street Apt 4\\nBuilding B;Austin;TX;78702;United States\r
item4.X-ABADR:US\r
ORG:Acme Corp;\r
TITLE:Engineer\r
NOTE:Met at a conference\r
item5.URL;type=pref:https://example.com\r
item5.X-ABLabel:_$!<HomePage>!$_\r
BDAY;value=date:1984-05-12\r
PHOTO;ENCODING=b;TYPE=JPEG:iVBORw0KGgo=\r
X-SOCIALPROFILE;type=twitter;x-user=janedoe:http://twitter.com/janedoe\r
X-SOCIALPROFILE;type=linkedin:https://www.linkedin.com/in/janedoe\r
item6.X-ABRELATEDNAMES;type=pref:John Doe\r
item6.X-ABLabel:_$!<Brother>!$_\r
X-ADDRESSBOOKSERVER-PHONEME-DATA:{"a":{"b":"c"}}\r
X-IMAGEHASH:abc123==\r
REV:2026-08-19T12:00:00Z\r
UID:11111111-1111-4111-8111-111111111111\r
END:VCARD\r
"""

MINIMAL = """BEGIN:VCARD\r
VERSION:3.0\r
N:Smith;Bob;;;\r
FN:Bob Smith\r
UID:22222222-2222-4222-8222-222222222222\r
END:VCARD\r
"""


def as_dict(text=RICH):
    return vcard.to_dict(vcard.parse(text), book_id="card", resource_name="x.vcf")


class TestReading:
    def test_maps_the_fields_the_tool_surface_promises(self):
        c = as_dict()
        assert c["name"] == "Jane Doe"
        assert c["organization"] == "Acme Corp"
        assert c["jobTitle"] == "Engineer"
        assert c["birthday"] == "1984-05-12"
        assert c["notes"] == "Met at a conference"
        assert c["kind"] == "individual"

    def test_the_preferred_entry_comes_first(self):
        # A caller taking emails[0] must get the intended address, not an
        # arbitrary one.
        c = as_dict()
        assert c["emails"][0] == "jane@example.com"
        assert c["phones"][0] == "(555) 010-0100"

    def test_apple_wrapped_labels_are_unwrapped(self):
        labels = {e["value"]: e.get("label") for e in c_labeled("emails")}
        assert labels["jane.doe@work.example"] == "Work"

    def test_user_defined_labels_are_kept_as_written(self):
        # Apple wraps its own labels in _$!<>!$_ but leaves custom ones bare.
        labels = {p["value"]: p.get("label") for p in c_labeled("phones")}
        assert labels["(555) 010-0200"] == "Google Voice"

    def test_a_label_falls_back_to_the_type_parameter(self):
        labels = {p["value"]: p.get("label") for p in c_labeled("phones")}
        assert labels["(555) 010-0100"] == "Cell"        # not INTERNET/VOICE noise

    def test_the_photo_is_never_reported(self):
        # Embedded photos are large enough to swamp a result set.
        assert "PHOTO" not in str(as_dict())
        assert "iVBORw0KGgo" not in str(as_dict())

    def test_internal_bookkeeping_is_not_reported(self):
        rendered = str(as_dict())
        for noise in ("PHONEME", "IMAGEHASH", "PRODID", "X-ABADR"):
            assert noise not in rendered

    def test_unmodelled_properties_are_surfaced_rather_than_dropped(self):
        # A card saying "Brother: John Doe" is answering a question somebody
        # might ask; silently discarding it pretends the data is not there.
        other = as_dict()["other"]
        assert other["Brother"] == ["John Doe"]
        assert any("twitter.com/janedoe" in v for v in other["Twitter"])

    def test_a_contact_with_nothing_extra_has_no_other_section(self):
        assert "other" not in as_dict(MINIMAL)

    def test_the_id_round_trips(self):
        book, resource, _ = ids.decode(as_dict()["id"], kind="contact")
        assert (book, resource) == ("card", "x.vcf")


def c_labeled(field):
    return as_dict()["labeled"][field]


class TestAddresses:
    def test_both_structured_and_formatted_shapes_are_returned(self):
        address = as_dict()["addresses"][0]
        assert address["city"] == "Austin"
        assert address["region"] == "TX"
        assert address["postcode"] == "78702"
        assert address["formatted"].startswith("100 Main Street Apt 4 Building B, Austin")

    def test_a_multi_line_street_keeps_its_break_in_the_components(self):
        # Real cards carry \\n-escaped streets. A mailing label wants the break.
        assert "\n" in as_dict()["addresses"][0]["street"]

    def test_but_the_formatted_line_is_single_line(self):
        # This one gets pasted into an event location.
        assert "\n" not in as_dict()["addresses"][0]["formatted"]

    def test_the_address_label_is_resolved(self):
        assert as_dict()["addresses"][0]["label"] == "Home"


class TestBuilding:
    def test_writes_vcard_3_which_is_what_apple_writes(self):
        assert "VERSION:3.0" in vcard.build(uid="U1", name="Jane Doe")

    def test_the_first_email_and_phone_become_preferred(self):
        body = vcard.build(
            uid="U1", name="Jane Doe",
            emails=["first@example.com", "second@example.com"],
            phones=["+1 555 0100", "+1 555 0200"],
        )
        parsed = vcard.to_dict(vcard.parse(body), book_id="b", resource_name="x.vcf")
        assert parsed["emails"][0] == "first@example.com"
        assert parsed["phones"][0] == "+1 555 0100"

    def test_a_structured_name_is_derived(self):
        # vCard 3.0 requires N, and some clients render a blank entry without it.
        body = vcard.build(uid="U1", name="Jane Doe")
        assert "N:Doe;Jane" in body

    def test_a_single_word_name_still_produces_a_card(self):
        assert "FN:Prince" in vcard.build(uid="U1", name="Prince")


class TestMutationPreservesTheCard:
    """The central guarantee: an edit must not delete what it does not model.

    Rebuilding a card from a parsed dict would silently drop the photo, the
    social profiles, the related names and Apple's own bookkeeping. Edits
    therefore mutate the parsed card in place.
    """

    def edited(self):
        card = vcard.parse(RICH)
        vcard.set_text(card, "FN", "Jane Doe-Smith")
        vcard.add_values(card, "EMAIL", ["new@example.com"], ["INTERNET"])
        vcard.touch(card)
        return card.serialize()

    @pytest.mark.parametrize(
        "fragment",
        [
            "PHOTO",
            "X-SOCIALPROFILE",
            "linkedin.com/in/janedoe",
            "X-ABRELATEDNAMES",
            "X-ADDRESSBOOKSERVER-PHONEME-DATA",
            "X-IMAGEHASH",
            "item2.X-ABLABEL",
            "TITLE:Engineer",
        ],
    )
    def test_untouched_material_survives_an_edit(self, fragment):
        assert fragment in self.edited()

    def test_the_edit_itself_applied(self):
        body = self.edited()
        assert "FN:Jane Doe-Smith" in body
        assert "new@example.com" in body

    def test_rev_is_stamped(self):
        assert self.edited().count("REV:") == 1

    def test_the_uid_is_never_changed(self):
        assert "UID:11111111-1111-4111-8111-111111111111" in self.edited()


class TestValueDeltas:
    def test_adding_a_duplicate_is_skipped(self):
        card = vcard.parse(RICH)
        added = vcard.add_values(card, "EMAIL", ["JANE@EXAMPLE.COM"], ["INTERNET"])
        assert added == []

    def test_removal_is_case_insensitive(self):
        card = vcard.parse(RICH)
        removed = vcard.remove_values(card, "EMAIL", ["JANE.DOE@WORK.EXAMPLE"])
        assert removed == ["jane.doe@work.example"]
        assert "jane.doe@work.example" not in card.serialize()

    def test_removing_a_grouped_value_takes_its_label_with_it(self):
        # Otherwise the card keeps an orphaned item2.X-ABLabel pointing at
        # nothing, which Apple's clients render oddly.
        card = vcard.parse(RICH)
        vcard.remove_values(card, "EMAIL", ["jane.doe@work.example"])
        body = card.serialize().upper()
        assert "ITEM2.X-ABLABEL" not in body
        assert "ITEM3.X-ABLABEL" in body        # the untouched one stays

    def test_removing_something_absent_is_a_no_op(self):
        card = vcard.parse(RICH)
        assert vcard.remove_values(card, "EMAIL", ["nobody@example.com"]) == []

    def test_clearing_a_text_field_removes_the_property(self):
        card = vcard.parse(RICH)
        vcard.set_text(card, "NOTE", "")
        assert "NOTE:" not in card.serialize()


class TestBadInput:
    def test_junk_is_rejected_with_a_readable_message(self):
        with pytest.raises(vcard.VCardError):
            vcard.parse("this is not a vcard")

    def test_a_calendar_object_is_not_a_vcard(self):
        with pytest.raises(vcard.VCardError):
            vcard.parse("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")


class TestMalformedCards:
    """Real address books hold 900+ cards written by many client versions.

    One bad card must not take out a whole search, so parsing is per-card and
    the caller skips what it cannot read.
    """

    def test_a_photo_with_broken_base64_fails_that_card_only(self):
        broken = RICH.replace("iVBORw0KGgo=", "not-valid-base64!!")
        with pytest.raises(vcard.VCardError):
            vcard.parse(broken)
        # ...while a neighbouring card is unaffected.
        assert as_dict(MINIMAL)["name"] == "Bob Smith"
