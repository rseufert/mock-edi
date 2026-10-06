"""Elements the dictionary does not declare, but the standard does.

The mock's coverage is the commonly traded core of each segment, which is a
fair scope - SAC has sixteen elements in 004010 and the five declared here
carry almost every real allowance. Reporting the other eleven as error 3,
*too many data elements*, is not a fair scope: code 3 says the element does
not exist at that position, and there it does. A partner testing a correct
SAC against the mock learned that their document was broken.

So a segment declares how wide the standard makes it. A position past the
definition but inside that width is carried and not checked. Past the width,
code 3 is the truth and is still reported.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, schema, validate, x12
from mockedi.envelope import seg

from support import ACME, MockServerCase, x12_order


def findings_for(tag, *elements, po_number="WIDTH-TEST"):
    """Every finding an 850 carrying this segment produces."""
    payload = x12_order(po_number, extra=[seg(tag, *elements)])
    report = validate.validate(x12.parse(payload))
    return [line.strip() for line in ack.explain(report)]


def element_problems(tag, *elements, **kwargs):
    return [line for line in findings_for(tag, *elements, **kwargs)
            if "no element at position" in line]


# How many elements the standard gives every X12 segment the dictionary
# declares: (004010, 005010). Counted as the highest position in two published
# copies, which agree on every row (#202): Stedi's reference, at
# stedi.com/edi/x12-004010/segment/<TAG> and .../x12-005010/segment/<TAG>,
# and the bots translator's grammars, x12/4010/records004010.py and
# x12/5010/records005010.py in github.com/bots-edi/bots-grammars.
STANDARD = {
    "ST": (2, 3), "SE": (2, 2), "BEG": (12, 12), "BAK": (10, 10),
    "BCH": (16, 16), "BCA": (15, 15), "POC": (27, 27), "BSN": (7, 7),
    "BIG": (10, 11), "CUR": (21, 21), "REF": (4, 4), "PER": (9, 9),
    "FOB": (9, 9), "DTM": (6, 6), "N1": (6, 6), "N2": (2, 2), "N3": (2, 2),
    "N4": (6, 7), "PO1": (25, 25), "PID": (9, 9), "PO4": (18, 18),
    "ACK": (29, 29), "CTT": (7, 7), "HL": (4, 4), "TD1": (10, 10),
    "TD5": (15, 15), "TD3": (10, 10), "PRF": (7, 7), "LIN": (31, 31),
    "SN1": (8, 8), "IT1": (25, 25), "ITD": (15, 15), "TXI": (10, 10),
    "SAC": (16, 16), "TDS": (4, 4), "CAD": (9, 9), "BPR": (21, 21),
    "TRN": (4, 4), "ENT": (9, 9), "RMR": (8, 8), "ADX": (4, 4),
    "AK1": (2, 3), "AK2": (2, 3), "AK3": (4, 4), "AK4": (4, 4),
    "AK5": (6, 6), "AK9": (9, 9), "ISA": (16, 16), "GS": (8, 8),
    "GE": (2, 2), "IEA": (2, 2), "TA1": (5, 5),
}
VERSIONS = ("004010", "005010")


def declared(version):
    """Every X12 segment the dictionary declares, as it stands at a version."""
    found = {}
    for code in schema.X12_SETS:
        for use, _loop in schema.lookup("X12", code, version).uses():
            found[use.tag] = use.segment
    for use in schema.ENVELOPES["X12"]:
        found[use.tag] = use.segment
    found["TA1"] = schema.TA1
    return found


def beyond(definition, count):
    """Where a segment of `count` elements is told it has one too many."""
    item = seg(definition.tag, *["X"] * count)
    report = validate.MessageReport(code="850", control="0001")
    validate._check_elements(item, definition, "", report)
    return [element.position for finding in report.segments
            for element in finding.elements
            if "no element at position" in element.note]


class TheWidthTheStandardGives(unittest.TestCase):
    """Every declared segment is as wide as the standard makes it (#202)."""

    def test_the_table_covers_every_segment_the_dictionary_declares(self):
        for version in VERSIONS:
            self.assertEqual(set(declared(version)), set(STANDARD), version)

    def test_each_is_as_wide_as_the_standard_at_each_version(self):
        for index, version in enumerate(VERSIONS):
            for tag, definition in sorted(declared(version).items()):
                with self.subTest(version=version, tag=tag):
                    self.assertEqual(definition.width, STANDARD[tag][index])

    def test_a_full_width_segment_draws_no_finding(self):
        for index, version in enumerate(VERSIONS):
            for tag, definition in sorted(declared(version).items()):
                with self.subTest(version=version, tag=tag):
                    self.assertEqual(beyond(definition, STANDARD[tag][index]), [])

    def test_and_one_element_wider_is_still_error_3(self):
        for index, version in enumerate(VERSIONS):
            for tag, definition in sorted(declared(version).items()):
                with self.subTest(version=version, tag=tag):
                    width = STANDARD[tag][index]
                    self.assertEqual(beyond(definition, width + 1), [width + 1])

    def test_a_width_is_said_only_where_the_definition_stops_short(self):
        # If a segment ever gets completed, its full_width becomes noise and
        # should go - the guard is here so that nobody has to remember.
        for tag, definition in sorted(declared("004010").items()):
            if definition.full_width:
                with self.subTest(tag=tag):
                    self.assertLess(len(definition.elements), definition.width)

    def test_a_complete_definition_needs_no_width(self):
        # TDS declares all four of its elements, so its width is its length
        # with nothing extra said.
        self.assertEqual(schema.TDS.full_width, 0)
        self.assertEqual(schema.TDS.width, len(schema.TDS.elements))


class AnUndeclaredElementInsideTheWidth(unittest.TestCase):
    """Not checked, and not reported as wrong."""

    def test_a_sac_with_a_description_at_sac15_is_clean(self):
        self.assertEqual(
            element_problems("SAC", "C", "D240", "", "", "1500", "", "", "",
                             "", "", "", "", "", "", "Freight"), [])

    def test_an_n1_with_an_entity_relationship_is_clean(self):
        self.assertEqual(
            element_problems("N1", "ST", "Acme DC 4", "92", "ACME-DC4", "1",
                             "BY"), [])

    def test_a_td5_with_a_service_level_is_clean(self):
        self.assertEqual(
            element_problems("TD5", "B", "2", "UPSN", "M", "United Parcel",
                             "", "", "", "", "", "", "3D"), [])

    def test_a_po4_with_pallet_data_is_clean(self):
        self.assertEqual(
            element_problems("PO4", "12", "1", "EA", "CS", "G", "20", "LB",
                             "1.5", "CF", "10", "8", "6", "IN"), [])

    def test_a_po1_with_a_sixth_product_id_pair_is_clean(self):
        self.assertEqual(
            element_problems("PO1", "9", "10", "EA", "1.00", "",
                             "VP", "WIDGET-001", "UP", "076123400003",
                             "BP", "B1", "EN", "E1", "IN", "I1", "MG", "M1"),
            [])


class TheOrderThatWasRefused(unittest.TestCase):
    """The segments #202 was reported with, in the order it was reported on."""

    EXTRA = [seg("PER", "BD", "Jane Doe", "TE", "6145550100", "EM",
                 "jane@acme.example"),
             seg("N1", "SF", "Acme Plant 2", "92", "ACME-P2"),
             seg("N4", "Columbus", "OH", "43217", "US", "SL", "DOCK4"),
             seg("PID", "F", "", "", "", "Widget", "", "", "", "EN")]

    def test_a_contact_a_dock_and_a_language_are_not_too_many_elements(self):
        self.assertEqual(
            [line for line in findings_for("REF", "DP", "042", "Housewares", "",
                                           po_number="WIDTH-202")
             if "no element at position" in line], [])
        payload = x12_order("WIDTH-202", extra=self.EXTRA)
        report = validate.validate(x12.parse(payload))
        self.assertEqual([line for line in ack.explain(report)
                          if "no element at position" in line], [])

    def test_n407_is_an_element_in_005010_and_not_in_004010(self):
        full = ["Columbus", "OH", "43217", "US", "SL", "DOCK4", "OH"]
        self.assertEqual(beyond(declared("005010")["N4"], 7), [])
        self.assertEqual(beyond(declared("004010")["N4"], 7), [7])
        self.assertEqual(len(full), 7)


class AnElementBeyondTheWidth(unittest.TestCase):
    """Still reported, because there the element really does not exist."""

    def test_a_sac_with_a_seventeenth_element_is_error_3(self):
        problems = element_problems(
            "SAC", *(["C", "D240", "", "", "1500"] + [""] * 10
                     + ["Freight", "beyond"]))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("position 17", problems[0])

    def test_an_n1_with_a_seventh_element_is_error_3(self):
        problems = element_problems("N1", "ST", "Acme DC 4", "92", "ACME-DC4",
                                    "1", "BY", "beyond")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("position 7", problems[0])

    def test_the_code_is_3_on_the_wire(self):
        payload = x12_order("WIDTH-WIRE", extra=[
            seg("N1", "ST", "Acme DC 4", "92", "ACME-DC4", "1", "BY", "beyond")])
        report = validate.validate(x12.parse(payload))
        codes = [element.code
                 for message in report.messages
                 for finding in message.segments
                 for element in finding.elements
                 if element.position == 7]
        self.assertEqual(codes, ["3"])


class TheEdifactSideToo(unittest.TestCase):
    """PIA carries up to five item numbers; the mock declares one."""

    def test_a_pia_with_three_item_numbers_is_clean(self):
        from mockedi import edifact
        from support import edifact_order
        payload = edifact_order("PIA-WIDTH", extra=[
            seg("PIA", "1", ["4711", "SA"], ["123456", "BP"], ["9012", "EN"])])
        report = validate.validate(edifact.parse(payload))
        self.assertEqual(ack.explain(report), ["ORDERS/1: accepted"])

    def test_a_pia_beyond_five_item_numbers_is_reported(self):
        from mockedi import edifact
        from support import edifact_order
        payload = edifact_order("PIA-BEYOND", extra=[
            seg("PIA", "1", *[["X%d" % n, "SA"] for n in range(6)])])
        report = validate.validate(edifact.parse(payload))
        self.assertTrue(any("position 7" in line for line in ack.explain(report)),
                        ack.explain(report))

    def test_c212_is_declared_the_same_way_wherever_it_appears(self):
        # The two definitions disagreeing is how the short one was found.
        def components(segment, position):
            return tuple(c.ref for c in segment.elements[position - 1].components)
        self.assertEqual(components(schema.PIA, 2), components(schema.LIN_E, 3))


class TheDictionarySaysWhichIsWhich(MockServerCase):
    """A guide writer needs to know an unchecked position from a wrong one."""

    def test_a_short_segment_reports_both_numbers(self):
        _status, _headers, data = self.get("/_mock/dictionary/X12/850")
        rows = {row["tag"]: row for row in data["segments"]}
        self.assertEqual(rows["N1"]["width"], 6)
        self.assertEqual(rows["N1"]["checkedTo"], 4)

    def test_a_complete_segment_reports_the_same_number_twice(self):
        _status, _headers, data = self.get("/_mock/dictionary/X12/850")
        rows = {row["tag"]: row for row in data["segments"]}
        self.assertEqual(rows["N3"]["width"], rows["N3"]["checkedTo"])

    def test_a_segment_that_was_narrow_says_how_far_it_is_checked(self):
        _status, _headers, data = self.get("/_mock/dictionary/X12/850")
        rows = {row["tag"]: row for row in data["segments"]}
        self.assertEqual((rows["PER"]["width"], rows["PER"]["checkedTo"]), (9, 4))
        self.assertEqual((rows["BEG"]["width"], rows["BEG"]["checkedTo"]), (12, 7))


class TheOrderIsStillProcessed(MockServerCase):
    """The point of the change: a correct document is accepted, not annotated."""

    def test_an_850_carrying_a_full_sac_is_accepted_clean(self):
        payload = x12_order("SAC-CLEAN", extra=[
            seg("SAC", "C", "D240", "", "", "1500", "", "", "", "", "", "",
                "", "", "", "Freight")])
        summary = self.send(payload)
        self.assertTrue(summary["accepted"])
        self.assertEqual(summary["transactionSets"][0]["findings"], [])

    def test_its_997_says_accepted_with_no_ak3(self):
        self.send(x12_order("SAC-CLEAN-997", extra=[
            seg("SAC", "C", "D240", "", "", "1500", "", "", "", "", "", "",
                "", "", "", "Freight")]))
        message = self.document(ACME, "acknowledgment").groups[0].messages[0]
        self.assertEqual([item.tag for item in message.segments
                          if item.tag == "AK3"], [])
        self.assertEqual(message.find("AK5").get(1), "A")


if __name__ == "__main__":
    unittest.main()
