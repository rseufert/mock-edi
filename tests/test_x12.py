"""The X12 envelope: fixed-width ISA, learned delimiters, control numbers."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mockedi import x12
from mockedi.envelope import EdiSyntaxError, seg

BODY = [seg("BEG", "00", "SA", "PO4711", "", "20260924")]


def interchange(**kw):
    kw.setdefault("moment", datetime.datetime(2026, 9, 24, 10, 30))
    return x12.wrap([x12.message("850", "0001", BODY)], "ACME", "MOCKEDI",
                    "77", "88", "PO", **kw)


class TheIsaSegment(unittest.TestCase):
    """ISA is fixed width so a receiver can read the delimiters out of it."""

    def setUp(self):
        self.text = x12.render(interchange())
        self.isa = self.text.split("~")[0]

    def test_it_is_exactly_106_characters_including_its_terminator(self):
        self.assertEqual(len(self.isa) + 1, 106)

    def test_identifiers_are_padded_to_fifteen(self):
        self.assertIn("*ACME           *", self.isa)
        self.assertIn("*MOCKEDI        *", self.isa)

    def test_the_control_number_is_nine_digits_zero_filled(self):
        self.assertIn("*000000077*", self.isa)

    def test_isa16_declares_the_component_separator_and_is_not_escaped(self):
        self.assertTrue(self.isa.endswith(">"))

    def test_00401_puts_the_standards_identifier_in_isa11(self):
        self.assertIn("*U*00401*", self.isa)

    def test_00501_puts_the_repetition_separator_there_instead(self):
        text = x12.render(interchange(interchange_version="00501"))
        self.assertIn("*^*00501*", text.split("~")[0])


class LearningDelimiters(unittest.TestCase):
    def test_the_defaults_are_read_back(self):
        found = x12.read_delimiters(x12.render(interchange()))
        self.assertEqual((found.element, found.segment, found.component),
                         ("*", "~", ">"))

    def test_an_unusual_punctuation_set_parses(self):
        text = x12.render(interchange()).replace("*", "|").replace("~", "\n")
        parsed = x12.parse(text)
        self.assertEqual(parsed.codes(), ["850"])
        self.assertEqual(parsed.sender, "ACME")

    def test_a_document_that_is_not_x12_is_refused_by_name(self):
        with self.assertRaises(EdiSyntaxError):
            x12.parse("UNB+UNOC:3+A:ZZ+B:ZZ+260924:1030+1'")

    def test_an_isa_with_too_few_elements_says_how_many_it_found(self):
        with self.assertRaises(EdiSyntaxError) as caught:
            x12.parse("ISA*00*a*00*b*ZZ*A*ZZ*B~")
        self.assertIn("16", str(caught.exception))


class Structure(unittest.TestCase):
    def setUp(self):
        self.parsed = x12.parse(x12.render(interchange()))

    def test_the_envelope_round_trips(self):
        self.assertEqual(self.parsed.sender, "ACME")
        self.assertEqual(self.parsed.receiver, "MOCKEDI")
        self.assertEqual(self.parsed.control, "000000077")
        self.assertEqual(self.parsed.version, "00401")

    def test_the_group_carries_its_own_control_number(self):
        group = self.parsed.groups[0]
        self.assertEqual(group.functional_id, "PO")
        self.assertEqual(group.control, "88")
        self.assertEqual(group.version, "004010")

    def test_segment_positions_count_from_the_header(self):
        message = self.parsed.groups[0].messages[0]
        self.assertEqual([s.position for s in message.segments], [1, 2, 3])
        self.assertEqual(message.segments[0].tag, "ST")

    def test_se01_counts_the_header_and_trailer_too(self):
        message = self.parsed.groups[0].messages[0]
        self.assertEqual(message.find("SE").get(1), "3")
        self.assertEqual(len(message.segments), 3)

    def test_the_test_indicator_is_carried(self):
        text = x12.render(interchange(test=True))
        self.assertIn("*T*", text.split("~")[0])
        self.assertTrue(x12.parse(text).test)

    def test_a_newline_per_segment_changes_nothing_but_readability(self):
        compact = x12.render(interchange())
        pretty = x12.render(interchange(), newline=True)
        self.assertNotEqual(compact, pretty)
        self.assertEqual(x12.parse(compact).codes(), x12.parse(pretty).codes())


class SeveralMessages(unittest.TestCase):
    def test_a_group_may_hold_more_than_one_transaction_set(self):
        messages = [x12.message("850", "0001", BODY), x12.message("850", "0002", BODY)]
        parsed = x12.parse(x12.render(
            x12.wrap(messages, "ACME", "MOCKEDI", "1", "1", "PO")))
        self.assertEqual(parsed.message_count, 2)
        self.assertEqual([m.control for _g, m in parsed.messages()], ["0001", "0002"])

    def test_ge01_counts_them(self):
        messages = [x12.message("850", "0001", BODY), x12.message("850", "0002", BODY)]
        text = x12.render(x12.wrap(messages, "ACME", "MOCKEDI", "1", "1", "PO"))
        self.assertIn("GE*2*1~", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
