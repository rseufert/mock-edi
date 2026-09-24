"""Delimiters, escaping and the shape both dialects share."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mockedi.envelope import (Delimiters, EDIFACT_DEFAULTS, EdiSyntaxError,
                              X12_DEFAULTS, escape, parse_date, render_segment,
                              Seg, seg, sniff, split_elements, split_segments)


class Sniffing(unittest.TestCase):
    def test_recognises_each_dialect_by_its_first_segment(self):
        self.assertEqual(sniff("ISA*00*          *..."), "X12")
        self.assertEqual(sniff("UNB+UNOC:3+..."), "EDIFACT")
        self.assertEqual(sniff("UNA:+.? 'UNB+UNOC:3+..."), "EDIFACT")

    def test_leading_whitespace_does_not_hide_the_dialect(self):
        self.assertEqual(sniff("\r\n  ISA*00*"), "X12")

    def test_anything_else_is_refused_with_what_it_saw(self):
        with self.assertRaises(EdiSyntaxError) as caught:
            sniff('{"not": "edi"}')
        self.assertIn("ISA", str(caught.exception))
        self.assertIn('{"not"', str(caught.exception))


class Splitting(unittest.TestCase):
    def test_segments_split_on_the_terminator(self):
        parts = split_segments("AAA*1~BBB*2~", X12_DEFAULTS)
        self.assertEqual(parts, ["AAA*1", "BBB*2"])

    def test_newlines_between_segments_are_not_data(self):
        parts = split_segments("AAA*1~\nBBB*2~\n", X12_DEFAULTS)
        self.assertEqual(parts, ["AAA*1", "BBB*2"])

    def test_composite_elements_become_lists(self):
        elements = split_elements("UNB+UNOC:3+ME:ZZ", EDIFACT_DEFAULTS)
        self.assertEqual(elements, [["UNOC", "3"], ["ME", "ZZ"]])

    def test_a_simple_element_stays_a_string(self):
        self.assertEqual(split_elements("BGM+220", EDIFACT_DEFAULTS), ["220"])


class ReleaseCharacter(unittest.TestCase):
    """EDIFACT's `?` escapes the next character. X12 has no equivalent."""

    def test_an_escaped_delimiter_is_data_not_a_separator(self):
        parts = split_segments("BGM+220+PO?+1+9'", EDIFACT_DEFAULTS)
        self.assertEqual(len(parts), 1)
        elements = split_elements(parts[0], EDIFACT_DEFAULTS)
        self.assertEqual(elements, ["220", "PO+1", "9"])

    def test_an_escaped_segment_terminator_does_not_end_the_segment(self):
        parts = split_segments("FTX+AAO+++don?'t ship'", EDIFACT_DEFAULTS)
        self.assertEqual(len(parts), 1)
        self.assertEqual(split_elements(parts[0], EDIFACT_DEFAULTS)[3], "don't ship")

    def test_writing_escapes_what_reading_unescapes(self):
        original = "ACME+SONS: the 'best'?"
        written = escape(original, EDIFACT_DEFAULTS)
        parts = split_segments("FTX+" + written + "'", EDIFACT_DEFAULTS)
        self.assertEqual(split_elements(parts[0], EDIFACT_DEFAULTS)[0], original)

    def test_x12_strips_delimiters_because_it_cannot_escape_them(self):
        self.assertEqual(escape("A*B~C", X12_DEFAULTS), "A B C")


class Rendering(unittest.TestCase):
    def test_trailing_empty_elements_are_trimmed(self):
        self.assertEqual(render_segment(seg("REF", "VN", "123", "", ""), X12_DEFAULTS),
                         "REF*VN*123")

    def test_empty_elements_in_the_middle_are_kept(self):
        self.assertEqual(render_segment(seg("BEG", "00", "", "PO1"), X12_DEFAULTS),
                         "BEG*00**PO1")

    def test_composites_trim_their_own_trailing_components(self):
        self.assertEqual(
            render_segment(seg("LIN", "1", "", ["SKU", "VP", "", ""]), EDIFACT_DEFAULTS),
            "LIN+1++SKU:VP")


class Accessors(unittest.TestCase):
    def setUp(self):
        self.segment = Seg("NAD", ["BY", ["ACME", "", "92"], "", ["Acme Inc"]])

    def test_positions_are_one_based_as_the_standards_number_them(self):
        self.assertEqual(self.segment.get(1), "BY")

    def test_get_on_a_composite_returns_its_first_component(self):
        self.assertEqual(self.segment.get(2), "ACME")

    def test_components_are_one_based_too(self):
        self.assertEqual(self.segment.comp(2, 3), "92")

    def test_an_absent_component_is_empty_not_an_error(self):
        self.assertEqual(self.segment.comp(2, 9), "")
        self.assertEqual(self.segment.get(99), "")

    def test_has_distinguishes_empty_from_absent(self):
        self.assertFalse(self.segment.has(3))
        self.assertTrue(self.segment.has(4))


class Dates(unittest.TestCase):
    def test_eight_digit_dates(self):
        self.assertEqual(parse_date("20260924").isoformat(), "2026-09-24")

    def test_six_digit_dates_use_the_standards_century_window(self):
        self.assertEqual(parse_date("260924").isoformat(), "2026-09-24")
        self.assertEqual(parse_date("960924").isoformat(), "1996-09-24")

    def test_nonsense_is_none_rather_than_an_exception(self):
        self.assertIsNone(parse_date("2026-09-24"))
        self.assertIsNone(parse_date("20261324"))
        self.assertIsNone(parse_date(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
