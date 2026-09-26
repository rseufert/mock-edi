"""Delimiters a document does not declare, and lengths from the wrong version.

Three details no lenient receiver notices and a strict one might.

A delimiter is stripped out of data (X12) or escaped in it (EDIFACT), so
carrying one the document never declares corrupts ordinary values. 004010 has
no repetition separator - ISA11 is the standards identifier, always `U` - and
EDIFACT syntax 3 has none either, where UNA position 5 is reserved and written
as a space. `^` was being removed from 004010 data, and an asterisk in a
syntax 3 document was coming out as `?*` beside a UNA that declared no release
target for it.

And an element length borrowed from a later version is a lie the mock tells
in its own favour: it accepts what the partner's translator will reject.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, schema, validate, x12
from mockedi.envelope import (EDIFACT_DEFAULTS, X12_DEFAULTS, escape, seg)

from support import ACME, EURODIS, GLOBEX, MockServerCase, edifact_order, x12_order

EDIFACT = {"Content-Type": "application/edifact"}


class WhatCountsAsADelimiter(unittest.TestCase):
    def test_004010_has_no_repetition_separator(self):
        self.assertEqual(X12_DEFAULTS.repetition, "")
        self.assertNotIn("^", X12_DEFAULTS.all)

    def test_edifact_syntax_3_has_none_either(self):
        self.assertEqual(EDIFACT_DEFAULTS.repetition, "")
        self.assertNotIn("*", EDIFACT_DEFAULTS.all)

    def test_a_caret_survives_x12_escaping(self):
        self.assertEqual(escape("WIDGET^BLUE", X12_DEFAULTS), "WIDGET^BLUE")

    def test_an_asterisk_is_not_released_in_edifact(self):
        self.assertEqual(escape("2*3", EDIFACT_DEFAULTS), "2*3")

    def test_the_delimiters_that_are_real_are_still_handled(self):
        self.assertEqual(escape("A*B~C>D", X12_DEFAULTS), "A B C D")
        self.assertEqual(escape("A+B'C:D", EDIFACT_DEFAULTS), "A?+B?'C?:D")


class ReadingISA11(unittest.TestCase):
    def isa(self, version, eleventh):
        return ("ISA*00*          *00*          *ZZ*ACME           "
                "*ZZ*MOCKEDI        *260924*1030*%s*%s*000000001*0*T*>~"
                % (eleventh, version))

    def test_00401_declares_no_repetition_separator(self):
        self.assertEqual(x12.read_delimiters(self.isa("00401", "U")).repetition, "")

    def test_00501_declares_one(self):
        self.assertEqual(x12.read_delimiters(self.isa("00501", "^")).repetition, "^")

    def test_and_00501_falls_back_when_the_sender_left_it_out(self):
        self.assertEqual(x12.read_delimiters(self.isa("00501", "U")).repetition, "^")


class WritingIt(unittest.TestCase):
    """The version settles it, not whoever built the object."""

    def interchange(self, version):
        built = x12.wrap([x12.message("850", "0001", [seg("BEG", "00", "SA",
                                                          "PO1", "", "20260924")])],
                         "MOCKEDI", "ACME", "1", "1", "PO", version=version)
        built.version = version
        return built

    def test_a_00401_interchange_puts_u_in_isa11(self):
        text = x12.render(self.interchange("00401"))
        self.assertIn("*U*00401*", text.split("~")[0])

    def test_a_00501_interchange_puts_the_separator_there(self):
        text = x12.render(self.interchange("00501"))
        self.assertIn("*^*00501*", text.split("~")[0])

    def test_and_escapes_it_out_of_data_only_for_00501(self):
        # The mirror of the bug: where `^` *is* a delimiter it has to go.
        for version, expected in (("00401", "A^B"), ("00501", "A B")):
            built = self.interchange(version)
            built.groups[0].messages[0].segments[1] = seg(
                "BEG", "00", "SA", "A^B", "", "20260924")
            body = x12.render(built)
            self.assertIn(expected, body, version)


class OnTheWire(MockServerCase):
    """What a partner actually receives, and what the mock accepts back."""

    def test_a_caret_in_a_purchase_order_number_is_kept(self):
        self.send(x12_order("PO^A"))
        order = self.order("PO^A")
        self.assertEqual(order["po_number"], "PO^A")
        payload = self.document(ACME, "response")
        self.assertIn("PO^A", "".join(
            str(v) for m in payload.groups[0].messages
            for s in m.segments for v in s.elements))

    def test_a_005010_partner_still_gets_a_repetition_separator(self):
        self.send(x12_order("PO-5010", sender=GLOBEX))
        row = self.mailbox(GLOBEX, "response")[0]
        isa = row["payload"].replace("\n", "").split("~")[0].split("*")
        self.assertEqual(isa[12], "00501")     # ISA12, the envelope version
        self.assertEqual(isa[11], "^")         # ISA11, which 00501 has

    def test_an_asterisk_in_an_edifact_reference_is_not_released(self):
        self.send(edifact_order("PO*STAR"), headers=EDIFACT)
        row = self.mailbox(EURODIS, "acknowledgment")[0]
        self.assertNotIn("?*", row["payload"])


class LengthsFromTheRightVersion(unittest.TestCase):
    def test_ref02_is_30_in_004010_and_50_in_005010(self):
        self.assertEqual(schema.REF.elements[1].max_len, 30)
        self.assertEqual(schema.REF_005010.elements[1].max_len, 50)

    def test_a_004010_set_uses_the_004010_length(self):
        definition = schema.lookup("X12", "850")
        lengths = [use.segment.elements[1].max_len
                   for use, _loop in definition.uses() if use.tag == "REF"]
        self.assertTrue(lengths)
        self.assertEqual(set(lengths), {30})

    def test_rff_1154_is_the_d96a_length(self):
        components = {c.ref: c for c in schema.RFF.elements[0].components}
        self.assertEqual(components["1154"].max_len, 35)

    def test_a_reference_too_long_for_d96a_is_reported(self):
        payload = edifact_order("RFF-LONG",
                                extra=[seg("RFF", ["ON", "R" * 40])])
        report = validate.validate(edifact.parse(payload))
        notes = [element.note
                 for message in report.messages
                 for finding in message.segments
                 for element in finding.elements]
        self.assertTrue(any("35" in note for note in notes), notes)


if __name__ == "__main__":
    unittest.main()
