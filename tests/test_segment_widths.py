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


class TheWidthTheStandardGives(unittest.TestCase):
    """The nine segments the dictionary deliberately stops short of."""

    EXPECTED = {"SAC": 16, "PO4": 18, "TD5": 15, "ITD": 15, "N1": 6,
                "CTT": 7, "PO1": 25, "IT1": 25, "LIN": 31}

    def test_each_declares_it(self):
        for tag, width in self.EXPECTED.items():
            definition = getattr(schema, tag)
            self.assertEqual(definition.width, width, tag)

    def test_and_each_is_still_shorter_than_it(self):
        # If one of these ever gets completed, its full_width becomes noise
        # and should go - the guard is here so that nobody has to remember.
        for tag in self.EXPECTED:
            definition = getattr(schema, tag)
            self.assertLess(len(definition.elements), definition.width, tag)

    def test_a_complete_definition_needs_no_width(self):
        # BEG declares all five of its elements, so its width is its length
        # with nothing extra said.
        self.assertEqual(schema.BEG.full_width, 0)
        self.assertEqual(schema.BEG.width, len(schema.BEG.elements))


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
        self.assertEqual(rows["BEG"]["width"], rows["BEG"]["checkedTo"])


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
