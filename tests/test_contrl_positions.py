"""The positions a CONTRL reports, as the syntax standard counts them (#208).

`UCD`'s `S011` holds two numbers that look alike and are counted differently,
which is how the mock came to report one of them one low:

    0098, erroneous data element position in segment
      "The numerical count position of the simple or composite data element
       in error. The segment code and each following simple or composite data
       element defined in the segment description shall cause the count to be
       incremented. *The segment tag has position number 1.*"

    0104, erroneous component data element position
      "The numerical count position of the component data element in error.
       Each component data element position defined in the composite data
       element description shall cause the count to be incremented. *The
       count starts at 1.*"

So `QTY01` is 2 and `BGM03` is 4, while the second component of `C186` is 2 in
the mock's numbering and in the standard's alike. `UCS`'s 0096 is a third
rule again - "the numbering starts with, and includes, the UNH segment as
segment number 1" - and that one the mock already had right.

Quoted from two published copies of the service directory, since CONTRL is a
service message and not in D.96A: xedi's UN/EDIFACT SERVICE_V3 CONTRL, and
GEFEG's JWG1 archive for syntax version 3 (composite S011 and the service
segment pages), which agree word for word.

The numbers below are the standard's, worked out from those definitions by
hand. A test that read them off `ack.py` would have agreed with the bug.

Five of the eight fail with `mockedi/` at the commit before this one. The
other three are pins, and two of them are the half of #208 the mock already
had right: 0096 counting the UNH as 1, and 0104 counting components from 1.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, edifact, validate


def contrl(body):
    """The CONTRL the mock answers one ORDERS with, as segments."""
    payload = ("UNB+UNOA:3+EURODIS+MOCKEDI+260924:1200+1'"
               "UNH+1+ORDERS:D:96A:UN'" + body +
               "UNT+10+1'UNZ+1+1'")
    interchange = edifact.parse(payload)
    report = validate.validate(interchange)
    return ack.syntax_report(interchange, report, report.messages)


GOOD_LINES = ("DTM+137:20260924:102'"
              "NAD+BY+ACME::91'"
              "NAD+SU+EURODIS::91'"
              "LIN+1++WIDGET-001:VP'")


def ucds(segments):
    return [s for s in segments if s.tag == "UCD"]


def ucss(segments):
    return [s for s in segments if s.tag == "UCS"]


class TheNumbersCONTRLReports(unittest.TestCase):
    """One bad element in each of two segments, and the three counts."""

    SEGMENTS = ("BGM+220+PO1+ZZ'" + GOOD_LINES + "QTY+21:lots'"
                "UNS+S'CNT+2:1'")

    def setUp(self):
        self.segments = contrl(self.SEGMENTS)

    def test_a_simple_element_counts_the_segment_tag(self):
        # 1225 is BGM's third data element, so 0098 is 4.
        bad_bgm = ucds(self.segments)[0]
        self.assertEqual(bad_bgm.comp(2, 1), "4")

    def test_a_composite_counts_the_segment_tag_too(self):
        # C186 is QTY's first data element, so 0098 is 2.
        bad_qty = ucds(self.segments)[1]
        self.assertEqual(bad_qty.comp(2, 1), "2")

    def test_but_the_component_count_starts_at_one(self):
        # 6060 is the second component of C186, and 0104 has no tag to count.
        bad_qty = ucds(self.segments)[1]
        self.assertEqual(bad_qty.comp(2, 2), "2")

    def test_a_simple_element_names_no_component(self):
        bad_bgm = ucds(self.segments)[0]
        self.assertEqual(bad_bgm.comp(2, 2), "")

    def test_the_segment_count_includes_the_unh(self):
        # UNH 1, BGM 2, DTM 3, NAD 4, NAD 5, LIN 6, QTY 7.
        self.assertEqual([item.get(1) for item in ucss(self.segments)],
                         ["2", "7"])


class TheTwoNumbersAreNotTheSame(unittest.TestCase):
    """The case that makes the difference visible rather than coincidental."""

    def test_an_element_whose_position_equals_its_component(self):
        # QTY01's 0098 is 2 and 6060's 0104 is 2 - equal here by accident.
        # BGM's C002/1000 is the one that separates them: C002 is BGM's first
        # data element, so 0098 is 2, while 1000 is its fourth component, so
        # 0104 is 4. Two different rules, two different answers, one UCD.
        segments = contrl("BGM+220:::" + "x" * 36 + "+PO1+9'"
                          + GOOD_LINES + "QTY+21:1'UNS+S'CNT+2:1'")
        bad = ucds(segments)[0]
        self.assertEqual((bad.comp(2, 1), bad.comp(2, 2)), ("2", "4"))


class TheSameCompositeInsideUCI(unittest.TestCase):
    """`UCI07` is `S011` too, so it counts the same way.

    A refused envelope is answered by `UCI` alone - there is no `UCM` to put
    a `UCD` under - and the element it names goes in the same composite. Both
    places had to move, and this is the second.
    """

    def test_a_refused_envelope_names_the_element_the_same_way(self):
        interchange = edifact.parse(
            "UNB+UNOA:3+EURODIS+MOCKEDI+260924:1200+1'UNZ+1+9'")
        report = validate.validate(interchange)
        self.assertTrue(report.interchange_rejected)
        uci = ack.syntax_report(interchange, report, report.messages)[0]
        # The first fatal finding is about UNZ01, the message count, which
        # with the tag at 1 is position 2.
        self.assertEqual(uci.get(6), "UNZ")
        self.assertEqual(uci.comp(7, 1), "2")


class WhatTheBuyerSideSaysAboutIt(unittest.TestCase):
    """`reconcile` reports 0098 as it finds it, so its note moves with this.

    Worth a test because the note is read by people. It now says the
    standard's number, which is right and reads oddly to anyone who counts
    data elements the way a spec writes them (`BGM03`). Naming the element
    instead of numbering it is a presentation change and not this one's.
    """

    def test_the_note_carries_the_standards_number(self):
        from mockedi import reconcile
        segments = contrl(TheNumbersCONTRLReports.SEGMENTS)
        payload = edifact.render(edifact.wrap(
            [edifact.message("CONTRL", "1", segments)],
            "MOCKEDI", "EURODIS", "2"))
        # The CONTRL walker itself, rather than `answers`, which matches on
        # the interchange a stored document was sent in and has nothing to
        # match against here.
        _group, message = list(edifact.parse(payload).messages())[0]
        notes = "; ".join(item.note for item
                          in reconcile._read_contrl(message, "1"))
        self.assertIn("element 4", notes)
        self.assertIn("element 2", notes)


if __name__ == "__main__":
    unittest.main()
