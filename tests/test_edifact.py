"""The EDIFACT envelope: UNA, composites, and the release character."""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mockedi import edifact
from mockedi.envelope import Delimiters, EdiSyntaxError, seg

BODY = [seg("BGM", ["220"], ["PO4711"], "9"),
        seg("DTM", ["137", "20260924", "102"])]


def interchange(**kw):
    kw.setdefault("moment", datetime.datetime(2026, 9, 24, 10, 30))
    return edifact.wrap([edifact.message("ORDERS", "1", BODY)], "EURODIS",
                        "MOCKEDI", "9001", **kw)


class ServiceStringAdvice(unittest.TestCase):
    def test_una_is_written_and_declares_all_six_characters(self):
        self.assertTrue(edifact.render(interchange()).startswith("UNA:+.? '"))

    def test_una_is_read_back(self):
        found = edifact.read_delimiters(edifact.render(interchange()))
        self.assertEqual((found.component, found.element, found.release,
                          found.segment), (":", "+", "?", "'"))

    def test_without_una_the_defaults_apply(self):
        text = edifact.render(interchange(), una=False)
        self.assertTrue(text.startswith("UNB"))
        self.assertEqual(edifact.parse(text).codes(), ["ORDERS"])

    def test_an_unusual_punctuation_set_is_honoured(self):
        odd = Delimiters(segment="#", element="|", component="^", release="!",
                         decimal=".")
        text = edifact.render(interchange(delimiters=odd))
        self.assertTrue(text.startswith("UNA^|.! #"))
        self.assertEqual(edifact.parse(text).sender, "EURODIS")

    def test_a_truncated_una_is_refused(self):
        with self.assertRaises(EdiSyntaxError):
            edifact.read_delimiters("UNA:+")


class Structure(unittest.TestCase):
    def setUp(self):
        self.parsed = edifact.parse(edifact.render(interchange()))

    def test_the_envelope_round_trips(self):
        self.assertEqual(self.parsed.sender, "EURODIS")
        self.assertEqual(self.parsed.receiver, "MOCKEDI")
        self.assertEqual(self.parsed.control, "9001")
        self.assertEqual(self.parsed.version, "UNOC:3")

    def test_the_message_version_comes_from_unh(self):
        message = self.parsed.groups[0].messages[0]
        self.assertEqual(message.code, "ORDERS")
        self.assertEqual(message.version, "D:96A:UN")

    def test_unt_counts_every_segment_including_unh_and_unt(self):
        message = self.parsed.groups[0].messages[0]
        self.assertEqual(message.find("UNT").get(1), "4")
        self.assertEqual(len(message.segments), 4)

    def test_one_implicit_group_so_both_dialects_look_alike(self):
        self.assertEqual(len(self.parsed.groups), 1)
        self.assertTrue(self.parsed.groups[0].implicit)

    def test_the_test_indicator_is_carried(self):
        text = edifact.render(interchange(test=True))
        self.assertTrue(edifact.parse(text).test)


class Composites(unittest.TestCase):
    def test_a_composite_keeps_its_components_apart(self):
        parsed = edifact.parse(edifact.render(interchange()))
        bgm = parsed.groups[0].messages[0].find("BGM")
        self.assertEqual(bgm.comp(1, 1), "220")
        self.assertEqual(bgm.comp(2, 1), "PO4711")
        self.assertEqual(bgm.get(3), "9")

    def test_a_delimiter_inside_a_value_survives_the_round_trip(self):
        body = [seg("BGM", ["220"], ["PO+4711:A"], "9"),
                seg("FTX", "AAI", "", "", ["don't split this+here"])]
        text = edifact.render(edifact.wrap(
            [edifact.message("ORDERS", "1", body)], "EURODIS", "MOCKEDI", "1"))
        message = edifact.parse(text).groups[0].messages[0]
        self.assertEqual(message.find("BGM").comp(2, 1), "PO+4711:A")
        self.assertEqual(message.find("FTX").comp(4, 1), "don't split this+here")


class Refusals(unittest.TestCase):
    def test_an_x12_document_is_refused_by_name(self):
        with self.assertRaises(EdiSyntaxError) as caught:
            edifact.parse("UNB is missing'BGM+220'")
        self.assertIn("UNB", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
