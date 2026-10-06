"""What D.96A lets a composite's component carry, and what it does not.

The EDIFACT dictionary gave several components a *later* directory's maximum:
`6411` was declared `an..8` where D.96A has `an..3`, `7008` `an..256` where it
has `an..35`, `4440` `an..512` where it has `an..70`. So `QTY+21:1:BOXES'` and
a hundred-and-twenty-character item description were accepted here and refused
by a translator that knows only D.96A - while the X12 side caught both of
their equivalents, which is how the same request came to be refused for one
supplier and sent to the other (#239).

The machinery was never the problem: `validate.py` has always walked a
composite's components and worded the finding `C186/6411`. The declarations
were.

Every maximum below is D.96A's own, from the directory rather than from the
dictionary this tests: the segment pages at edifactory.de, corroborated by
xedi's D.96A message pages. UNECE's own service answers 403.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import schema
from mockedi.envelope import seg

from test_validate import check, notes

# The nine component maxima D.96A gives, and a segment that carries each one
# at a length the test chooses. Written from the directory: the numbers here
# are the source's, and a test that read them off `schema` would pass against
# the bug it is here to keep out.
#
#   label, element, D.96A maximum, where it sits, how to build the segment
WIDTHS = [
    ("QTY01/6060", "6060", 15, "detail", lambda v: seg("QTY", ["21", v])),
    ("QTY01/6411", "6411", 3, "detail", lambda v: seg("QTY", ["21", "1", v])),
    ("PRI01/6411", "6411", 3, "detail",
     lambda v: seg("PRI", ["AAA", "12.50", "", "", "", v])),
    ("CNT01/6411", "6411", 3, "summary", lambda v: seg("CNT", ["2", "1", v])),
    ("IMD03/7008", "7008", 35, "detail",
     lambda v: seg("IMD", "F", "", ["", "", "", v])),
    ("MOA01/5004", "5004", 18, "detail", lambda v: seg("MOA", ["203", v])),
    ("FTX04/4440", "4440", 70, "header",
     lambda v: seg("FTX", "AAO", "", "", [v])),
]

HEADER = [seg("BGM", ["220"], ["WIDTH-TEST"], "9"),
          seg("DTM", ["137", "20260924", "102"]),
          seg("NAD", "BY", ["ACME", "", "91"]),
          seg("NAD", "SU", ["EURODIS", "", "91"])]
LINE = [seg("LIN", "1", "", ["WIDGET-001", "VP"])]


def orders(where, segment):
    """An ORDERS carrying one segment, in a place the message admits it."""
    body = list(HEADER)
    if where == "header":
        body += [segment] + LINE
    elif where == "detail":
        body += LINE + [segment]
    else:
        body += LINE + [seg("UNS", "S"), segment]
        return body
    return body + [seg("UNS", "S"), seg("CNT", ["2", "1"])]


def findings(where, segment):
    return notes(check(orders(where, segment), code="ORDERS",
                       dialect="EDIFACT"))


def too_long(label, maximum):
    """The finding `validate.py` has always worded this way."""
    return "%s is %d characters, the maximum is %d" % (label, maximum + 1,
                                                       maximum)


class WhatD96AAllows(unittest.TestCase):
    """A component at its maximum passes; one character more does not.

    The first of these is a pin: a value at D.96A's maximum was accepted
    before this change too, because the declared maximum was larger. It is
    here so that correcting a maximum downwards cannot overshoot. The second
    is the evidence - all seven of its cases fail without the change.
    """

    def test_a_component_at_the_maximum_is_accepted(self):
        for label, _ref, maximum, where, build in WIDTHS:
            with self.subTest(label):
                value = ("1" * maximum if label.endswith(("6060", "5004"))
                         else "PCE" if label.endswith("6411")
                         else "x" * maximum)
                self.assertEqual(findings(where, build(value)), [], label)

    def test_one_character_more_is_a_finding(self):
        for label, _ref, maximum, where, build in WIDTHS:
            with self.subTest(label):
                value = ("1" * (maximum + 1)
                         if label.endswith(("6060", "5004"))
                         else "x" * (maximum + 1))
                self.assertIn(too_long(label, maximum),
                              findings(where, build(value)), label)


class TheTwoDocumentsTheIssueWasFiledWith(unittest.TestCase):
    """Verbatim from #239, which reported both as accepted."""

    def test_a_five_character_measure_unit_is_refused(self):
        found = findings("detail", seg("QTY", ["21", "1", "BOXES"]))
        self.assertIn("QTY01/6411 is 5 characters, the maximum is 3", found)

    def test_a_hundred_and_twenty_character_description_is_refused(self):
        found = findings("detail", seg("IMD", "F", "", ["", "", "", "x" * 120]))
        self.assertIn("IMD03/7008 is 120 characters, the maximum is 35", found)


class TheUnitsSixFourOneOneAccepts(unittest.TestCase):
    """X12's 355 carries a code list in all eight places; 6411 had none."""

    def test_a_unit_the_mock_can_translate_passes(self):
        for code in sorted(schema.UOM_FROM_EDIFACT):
            with self.subTest(code):
                self.assertEqual(
                    findings("detail", seg("QTY", ["21", "1", code])), [], code)

    def test_a_unit_it_cannot_is_a_finding(self):
        # LTR is a real Recommendation 20 code and D.96A admits it. The mock
        # refuses it, as the X12 side already refuses `LT`, and says which
        # units it does take. Loosening this means saying so on both sides.
        found = findings("detail", seg("QTY", ["21", "1", "LTR"]))
        self.assertTrue(any("LTR is not a code QTY01/6411 accepts" in note
                            for note in found), found)

    def test_the_code_list_and_the_translation_table_agree(self):
        # Two statements of which units the mock knows. A unit it can
        # translate but not validate - or the other way - is a mock that
        # contradicts itself, and nobody has to remember to check.
        self.assertEqual(set(schema.EDIFACT_UOM_CODES),
                         set(schema.UOM_FROM_EDIFACT))


class WhatTheMockItselfStillWrites(unittest.TestCase):
    """Pins, not evidence: these pass before the change as well as after.

    They are here because tightening a maximum is only safe while the mock's
    own documents stay inside it, and `/_mock/purchase` refuses what the
    dictionary would reject (#237). If a seed description ever grows past 35
    characters, this says so rather than a user's order being turned away.
    """

    def test_every_seeded_description_fits_7008(self):
        from mockedi.db import CATALOG
        for row in CATALOG:
            with self.subTest(row[0]):
                self.assertLessEqual(len(row[1]), 35, row[1])

    def test_every_translated_unit_fits_6411(self):
        for code in schema.UOM_TO_EDIFACT.values():
            with self.subTest(code):
                self.assertLessEqual(len(code), 3, code)

    def test_the_longest_reason_the_mock_writes_fits_4440(self):
        # The FTX the mock writes carries an order line's reason. The longest
        # is "Confirmed %s of %s; the balance is not available" - 48
        # characters before two quantities go in.
        reason = "Confirmed %s of %s; the balance is not available" % (
            "9" * 10, "9" * 10)
        self.assertLessEqual(len(reason), 70, reason)
        self.assertEqual(findings("header", seg("FTX", "AAO", "", "", [reason])),
                         [])


if __name__ == "__main__":
    unittest.main()
