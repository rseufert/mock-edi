"""An acknowledgment's note names the element it is about (#294).

A 997 or a CONTRL says where a fault is by number. The mock passed the
number straight into the note a person reads - `element 3: Data element too
long` - while its own findings name the element the way a trading partner's
guide does: `PO103`. After #208 a CONTRL's number counts the segment tag as
well, so `QTY01` reads `element 2`: right, and one step further from how
anyone reads a specification.

The note now names the element and keeps the number beside it, for whoever
is looking for it in the bytes.

A 997 can be named from itself: `AK3` carries the segment's tag. A CONTRL
cannot: `UCS` carries the segment's *position* and no tag, so naming needs
the message the CONTRL is about, which the mock sent and still holds. Where
it does not hold it, the note keeps the numbers.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, reconcile, x12
from mockedi.envelope import seg

from support import (ACME, EURODIS, MockServerCase, acknowledge, edifact_order,
                     x12_order)

EDI = {"Content-Type": "application/edifact"}


class A997(MockServerCase):

    def setUp(self):
        super().setUp()
        self.send(x12_order("NAMED-X12"))
        self.sent = {row["code"]: row for row in self.mailbox(ACME)}

    def note(self, code):
        _status, _headers, rows = self.get(
            "/_mock/documents?direction=out&code=" + code)
        return rows[0]["ack_note"]

    def test_the_element_is_named_from_the_ak3_above_it(self):
        self.send(acknowledge(self.sent["855"]["payload"], "E",
                              errors=[("PO1", 6, "8", 3, "5", "BOXES")]))
        self.assertEqual(
            self.note("855"),
            "PO1 at segment 6: Segment has data element errors; "
            "PO103 (element 3): Data element too long ('BOXES')")

    def test_the_timeline_says_it_in_the_same_words(self):
        self.send(acknowledge(self.sent["855"]["payload"], "E",
                              errors=[("PO1", 6, "8", 3, "5", "BOXES")]))
        notes = [event["note"] for event in self.mock.timeline("NAMED-X12")["events"]
                 if event["event"] == "acknowledged"]
        self.assertEqual(notes, [self.note("855")])


class TheWalkerOfA997(unittest.TestCase):

    def notes(self, *body):
        message = x12.message("997", "0001", [
            seg("AK1", "PR", "1"), seg("AK2", "855", "0001")] + list(body) + [
            seg("AK5", "E"), seg("AK9", "E", "1", "1", "1")])
        return [item.note for item in reconcile._read_997(message)]

    def test_a_component_position_is_named_too(self):
        self.assertEqual(
            self.notes(seg("AK3", "POC", "4", "", "8"),
                       seg("AK4", ["5", "1"], "", "7", "ZZ")),
            ["POC at segment 4: Segment has data element errors; "
             "POC05 component 1 (element 5:1): Invalid code value ('ZZ')"])

    def test_position_nought_is_not_an_element_to_name(self):
        # Found by Rusty: `AK4*0` was named PO100.
        self.assertEqual(
            self.notes(seg("AK3", "PO1", "6", "", "8"),
                       seg("AK4", "0", "", "5"))[0].split("; ")[1],
            "element 0: Data element too long")

    def test_with_no_ak3_there_is_no_tag_and_the_number_stands(self):
        self.assertEqual(self.notes(seg("AK4", "3", "", "5", "BOXES")),
                         ["element 3: Data element too long ('BOXES')"])

    def test_a_position_that_is_not_a_number_is_left_as_it_came(self):
        self.assertEqual(
            self.notes(seg("AK3", "PO1", "6", "", "8"),
                       seg("AK4", "x", "", "5"))[0].split("; ")[1],
            "element x: Data element too long")


class AContrl(MockServerCase):

    def setUp(self):
        super().setUp()
        self.send(edifact_order("NAMED-EDI"), headers=EDI)
        self.sent = {row["code"]: row for row in self.mailbox(EURODIS)}
        message = next(edifact.parse(self.sent["ORDRSP"]["payload"]).messages())[1]
        # Where the first QTY sits, counting the UNH as 1, as 0096 does.
        self.qty = [index for index, item in enumerate(message.segments, start=1)
                    if item.tag == "QTY"][0]

    def note(self, code="ORDRSP"):
        _status, _headers, rows = self.get(
            "/_mock/documents?direction=out&code=" + code)
        return rows[0]["ack_note"]

    def test_the_element_is_named_from_the_message_the_contrl_is_about(self):
        # UCD+12+2:1 - the first component of the second position, the tag
        # being the first: C186's 6063.
        self.send(acknowledge(self.sent["ORDRSP"]["payload"], "R",
                              errors=[("QTY", self.qty, "12", 2, "12", "")]),
                  headers=EDI)
        self.assertEqual(
            self.note(),
            "QTY at segment %d: Invalid value; "
            "QTY01/6063 (segment %d, element 2:1): Invalid value"
            % (self.qty, self.qty))

    def test_the_timeline_says_it_in_the_same_words(self):
        self.send(acknowledge(self.sent["ORDRSP"]["payload"], "R",
                              errors=[("QTY", self.qty, "12", 2, "12", "")]),
                  headers=EDI)
        notes = [event["note"] for event in self.mock.timeline("NAMED-EDI")["events"]
                 if event["event"] == "acknowledged"]
        self.assertEqual(notes, [self.note()])

    def test_the_mocks_own_contrl_is_named_on_the_timeline_too(self):
        # A CONTRL the mock sent, about an order with a fault in it: the
        # message it is about came in, and is looked up that way.
        faulty = edifact_order("NAMED-BAD").replace("QTY+21:", "QTY+ZZ:", 1)
        self.send(faulty, headers=EDI)
        answers = [event["answers"] for event
                   in self.mock.timeline("NAMED-BAD")["events"]
                   if event.get("code") == "CONTRL"]
        notes = [each["note"] for answer in answers for each in answer["sets"]]
        self.assertEqual(len(notes), 1, notes)
        self.assertRegex(notes[0], r"QTY01/6063 \(segment \d+, element 2:1\): "
                                   r"Invalid value")

    def test_a_segment_past_the_end_of_the_message_keeps_its_numbers(self):
        self.send(acknowledge(self.sent["ORDRSP"]["payload"], "R",
                              errors=[("QTY", 999, "12", 2, "12", "")]),
                  headers=EDI)
        self.assertEqual(
            self.note(),
            "segment 999: Invalid value; "
            "segment 999, element 2:1: Invalid value")


class TheWalkerOfAContrl(unittest.TestCase):

    def notes(self, *body, sent=None):
        message = edifact.message("CONTRL", "1", list(body))
        return [item.note for item in reconcile._read_contrl(message, "1", sent)]

    def test_with_no_message_to_look_at_the_numbers_stand(self):
        self.assertEqual(
            self.notes(seg("UCI", "1", ["A"], ["B"], "7"),
                       seg("UCM", "1", ["ORDRSP", "D", "96A", "UN"], "4"),
                       seg("UCS", "4", "12"),
                       seg("UCD", "12", ["2", "2"])),
            ["segment 4: Invalid value; segment 4, element 2:2: Invalid value"])

    def test_a_component_past_the_composite_is_a_position_in_words(self):
        # Found by Rusty: it read QTY01/9, which looks like a directory number.
        about = edifact.message("ORDRSP", "1", [
            seg("BGM", ["231"], "R1", "29"), seg("QTY", ["21", "10", "PCE"])])
        position = [index for index, item in enumerate(about.segments, start=1)
                    if item.tag == "QTY"][0]
        (note,) = self.notes(
            seg("UCI", "1", ["A"], ["B"], "7"),
            seg("UCM", "1", ["ORDRSP", "D", "96A", "UN"], "4"),
            seg("UCS", str(position), "12"),
            seg("UCD", "16", ["2", "9"]),
            sent=lambda code, control, interchange: about)
        self.assertIn("QTY01 component 9 (segment %d, element 2:9): "
                      "Too many constituents" % position, note)

    def test_a_refused_envelope_names_its_service_segment_from_the_uci(self):
        (note,) = self.notes(
            seg("UCI", "1", ["A"], ["B"], "4", "29", "UNZ", ["2"]))
        self.assertTrue(note.endswith(
            "; UNZ01 (element 2): Control count does not match number of "
            "instances received"), note)

    def test_the_label_is_one_less_than_0098_because_0098_counts_the_tag(self):
        body = [seg("UNH", "1", ["ORDRSP", "D", "96A", "UN"]),
                seg("BGM", ["231"], "R1", "29")]
        about = edifact.message("ORDRSP", "1", body[1:])
        position = [index for index, item in enumerate(about.segments, start=1)
                    if item.tag == "BGM"][0]
        (note,) = self.notes(
            seg("UCI", "1", ["A"], ["B"], "7"),
            seg("UCM", "1", ["ORDRSP", "D", "96A", "UN"], "4"),
            seg("UCS", str(position), "12"),
            seg("UCD", "12", ["4"]),
            sent=lambda code, control, interchange: about)
        self.assertIn("BGM03 (segment %d, element 4): Invalid value" % position,
                      note)


if __name__ == "__main__":
    unittest.main()
