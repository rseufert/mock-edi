"""A simple element that arrives in pieces is said to have (#288).

The parser splits an element on the component separator wherever it finds
one; it does not know which elements are composites. So an element the
dictionary declares as simple, sent with a separator in it, reached the
validator as a list, and the validator read the first piece and dropped the
rest without a word: `REF*ZZ*A>B` was `REF02 = "A"`. Every other malformed
element drew a finding. This one drew silence, and the mock read a document
differently from how it was sent.

It is reported now, in both dialects, as an error and not a fatal one: the
first piece is still a reading, and it is still what is carried forward.

It is reported only for a position the dictionary declares. A position past
the declaration and inside the segment's width is carried and not judged,
because there the standard may well have a composite - `REF04` is one.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, edifact, schema, transactions, validate, x12
from mockedi.envelope import seg

from support import (ACME, EURODIS, MockServerCase, edifact_order, x12_change,
                     x12_order)

PIECES = "is a simple element and arrived with"


def said(payload, parse=x12.parse):
    report = validate.validate(parse(payload))
    return [line.strip() for line in ack.explain(report) if PIECES in line]


def checked(definition, *elements):
    """The element findings one segment draws, by its definition alone."""
    item = seg(definition.tag, *elements)
    report = validate.MessageReport(code="850", control="0001")
    validate._check_elements(item, definition, "", report)
    return [element for finding in report.segments
            for element in finding.elements]


class InX12(unittest.TestCase):

    ORDER = x12_order("PIECES-X12", extra=[seg("REF", "ZZ", ["A", "B"])])

    def test_the_separator_in_a_simple_element_is_a_finding(self):
        self.assertIn("REF*ZZ*A>B", self.ORDER)
        (line,) = said(self.ORDER)
        self.assertTrue(line.endswith(
            "REF02 is a simple element and arrived with 2 components "
            "('A', 'B'); read as 'A'"), line)

    def test_it_is_code_6_and_an_error_not_a_fatal_one(self):
        (finding,) = checked(schema.REF, "ZZ", ["A", "B"])
        self.assertEqual((finding.position, finding.code, finding.severity),
                         (2, "6", validate.ERROR))
        self.assertEqual(schema.ELEMENT_ERROR_CODES[finding.code],
                         "Invalid character in data element")

    def test_the_first_piece_is_still_what_is_read(self):
        order = transactions.read_order(
            x12.parse(x12_order("PIECES-READ", extra=[])).groups[0].messages[0],
            "X12")
        self.assertEqual(order.po_number, "PIECES-READ")
        (finding,) = checked(schema.REF, ["ZZ", "X"], "A")
        # REF01 is a code: the first piece is judged as the code it is.
        self.assertEqual(finding.code, "6")

    def test_three_pieces_are_counted_and_named(self):
        (finding,) = checked(schema.REF, "ZZ", ["A", "B", "C"])
        self.assertIn("arrived with 3 components ('A', 'B', 'C')", finding.note)

    def test_an_empty_piece_after_the_separator_is_not_a_second_one(self):
        self.assertEqual(checked(schema.REF, "ZZ", ["A", ""]), [])
        self.assertEqual(checked(schema.REF, "ZZ", ["A"]), [])

    def test_the_other_checks_on_the_element_still_run(self):
        found = checked(schema.REF, "ZZ", ["A" * 60, "B"])
        self.assertEqual(sorted(finding.code for finding in found), ["5", "6"])


class WhereTheStandardHasAComposite(unittest.TestCase):
    """Declared as one, or not declared at all: neither is reported."""

    def test_ref04_past_the_declaration_draws_nothing(self):
        self.assertEqual(
            checked(schema.REF, "ZZ", "A", "described", ["DP", "042"]), [])
        order = x12_order("PIECES-REF04", extra=[
            seg("REF", "ZZ", "A", "described", ["DP", "042"])])
        self.assertEqual(said(order), [])

    def test_no_position_past_a_declaration_is_judged(self):
        for code in schema.X12_SETS:
            for use, _loop in schema.lookup("X12", code).uses():
                definition = use.segment
                declared = len(definition.elements)
                if definition.width <= declared:
                    continue
                with self.subTest(segment=definition.tag):
                    elements = [""] * declared + [["X", "Y"]]
                    found = [finding for finding in checked(definition, *elements)
                             if PIECES in finding.note]
                    self.assertEqual(found, [])

    def test_poc05_is_the_composite_unit_of_measure(self):
        element = schema.POC.element(5)
        self.assertEqual((element.ref, element.composite), ("C001", True))
        self.assertEqual(
            checked(schema.POC, "1", "QD", "60", "", ["EA", "2"], "12.50"), [])

    def test_a_plain_unit_in_poc05_reads_as_it_did(self):
        change = transactions.read_change(x12.parse(x12_change(
            "PIECES-POC", [("1", "QD", 60, "12.50")])).groups[0].messages[0], "X12")
        self.assertEqual(change.lines[0].uom, "EA")
        self.assertEqual(checked(schema.POC, "1", "QD", "60", "", "EA", "12.50"), [])
        self.assertEqual(
            [finding.code for finding in
             checked(schema.POC, "1", "QD", "60", "", "ZZ", "12.50")], ["7"])

    def test_ak401_is_the_position_composite(self):
        element = schema.AK4.element(1)
        self.assertEqual((element.ref, element.composite), ("C030", True))
        self.assertEqual(checked(schema.AK4, ["2", "1"], "", "7", "ZZ"), [])
        self.assertEqual(checked(schema.AK4, "2", "", "7", "ZZ"), [])


class InEdifact(unittest.TestCase):

    def order(self):
        payload = edifact_order("PIECES-EDI")
        # 1225, the message function, is a simple element: give it a second
        # component, as a later directory's composite would.
        self.assertIn("+PIECES-EDI+9'", payload)
        return payload.replace("+PIECES-EDI+9'", "+PIECES-EDI+9:X'")

    def test_it_is_a_finding_there_too(self):
        (line,) = said(self.order(), edifact.parse)
        self.assertTrue(line.endswith(
            "BGM03 is a simple element and arrived with 2 components "
            "('9', 'X'); read as '9'"), line)

    def test_the_contrl_says_too_many_constituents(self):
        interchange = edifact.parse(self.order())
        report = validate.validate(interchange)
        segments = ack.syntax_report(interchange, report, report.messages)
        codes = [item.get(1) for item in segments if item.tag == "UCD"]
        self.assertEqual(codes, ["16"])
        self.assertEqual(schema.EDIFACT_SYNTAX_ERRORS["16"], "Too many constituents")

    def test_a_released_separator_is_one_value_and_draws_nothing(self):
        payload = edifact_order("PIECES-ESC").replace(
            "Hafenstrasse 12", "Hafenstrasse 12?: Tor 4")
        self.assertEqual(said(payload, edifact.parse), [])


class OnTheWire(MockServerCase):

    def test_an_x12_order_is_accepted_with_errors_and_the_997_names_the_element(self):
        summary = self.send(x12_order("PIECES-997", extra=[
            seg("REF", "ZZ", ["A", "B"])]))
        self.assertTrue(summary["accepted"])
        message = self.document(ACME, "acknowledgment").groups[0].messages[0]
        ak4 = message.find("AK4")
        self.assertEqual((ak4.get(1), ak4.get(3)), ("2", "6"))
        self.assertEqual(message.find("AK5").get(1), "E")

    def test_a_strict_partner_rejects_it(self):
        self.patch("/_mock/partners/" + ACME, {"behaviour": "strict"})
        summary = self.send(x12_order("PIECES-STRICT", extra=[
            seg("REF", "ZZ", ["A", "B"])]))
        self.assertFalse(summary["accepted"])


class WhatTheMockWritesItself(MockServerCase):
    """None of its own documents puts a separator in a simple element."""

    def test_an_x12_flow_and_a_change(self):
        self.send(x12_order("PIECES-OWN"))
        for row in self.mailbox(ACME):
            with self.subTest(code=row["code"]):
                self.assertEqual(said(row["payload"]), [])

    def test_an_edifact_flow(self):
        self.send(edifact_order("PIECES-OWN-E"))
        for row in self.mailbox(EURODIS):
            with self.subTest(code=row["code"]):
                self.assertEqual(said(row["payload"], edifact.parse), [])

    def test_the_orders_and_changes_it_places(self):
        for partner, dialect in (("SELLX", "X12"), ("SELLE", "EDIFACT")):
            self.post("/_mock/partners", {
                "id": partner, "name": partner, "role": "supplier",
                "dialect": dialect,
                "version": "004010" if dialect == "X12" else "D:96A:UN"})
            po_number = "PIECES-BUY-" + partner
            self.post("/_mock/purchase", {
                "partner": partner, "po_number": po_number,
                "lines": [{"sku": "WIDGET-001", "quantity": "10", "uom": "EA",
                           "price": "12.50"}]})
            status, _headers, data = self.post(
                "/_mock/purchase/%s/change" % po_number,
                {"partner": partner,
                 "lines": [{"line": "1", "quantity": "6"}]})
            self.assertIn(status, (200, 201), data)
            rows = self.mailbox(partner)
            self.assertGreaterEqual(len(rows), 2, rows)
            parse = x12.parse if dialect == "X12" else edifact.parse
            for row in rows:
                with self.subTest(partner=partner, code=row["code"]):
                    self.assertEqual(said(row["payload"], parse), [])


if __name__ == "__main__":
    unittest.main()
