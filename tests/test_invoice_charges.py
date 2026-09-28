"""An invoice's allowances and charges, read before its total is judged (#158).

A correct invoice billing 1250.00 of goods and a 50.00 freight charge states a
total of 1300.00. Before this, the X12 reader ignored SAC and #152's
`total-not-lines` called that a disagreement; the EDIFACT dictionary had no
ALC at all, so the INVOIC failed validation outright.
"""
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, schema, transactions, x12
from mockedi.envelope import seg
from mockedi.transactions import Party

from support import _next_control, parse
from test_three_way_match import LINE_1, WHEN, MatchCase, bill

GOODS = "1250.00"


def invoice(dialect, sender, total, line_charges=(), charges=(), subtotal=None,
            tax="0.00"):
    """An 810 or INVOIC for line 1, with allowances and charges put where the
    standards put them. Each charge is `(indicator, amount)`: C or A, and an
    amount, or None for one given only as a percentage."""
    supplier = Party(role="SE", name="Supplier", identifier=sender, country="US")
    mock = {"id": "MOCKEDI", "name": "Mock EDI", "qualifier": "ZZ", "street": "",
            "city": "", "region": "", "postal": "", "country": "US", "duns": "",
            "dialect": dialect, "version": "004010"}
    order = {"po_number": "PO-B", "seller_order": "SO-1", "ordered_on": "2026-09-27",
             "currency": "USD", "ship_to_name": "Mock EDI", "ship_to_id": "MOCKEDI"}
    shipment = {"shipment_id": "SH-1", "shipped_on": "2026-09-28", "tracking": "1Z",
                "bol": "B", "carrier": "UPS", "scac": "UPSN", "cartons": "1",
                "weight": "1"}
    facts = bill("INV-C", LINE_1, total=total, tax=tax,
                 subtotal=subtotal if subtotal is not None else GOODS)
    body = transactions.write_invoice(dialect, supplier, mock, order, LINE_1,
                                      facts, shipment, WHEN)
    code = schema.set_code(dialect, schema.INVOICE)
    control = _next_control(9)
    if dialect == "X12":
        def sac(indicator, amount):
            return seg("SAC", indicator, "D240", "", "", "" if amount is None
                       else str(int(Decimal(amount) * 100)),
                       "", "" if amount is not None else "3")
        it1 = [i for i, item in enumerate(body) if item.tag == "IT1"][0]
        for n, (indicator, amount) in enumerate(line_charges, 1):
            body.insert(it1 + n, sac(indicator, amount))
        tds = [i for i, item in enumerate(body) if item.tag == "TDS"][0]
        for n, (indicator, amount) in enumerate(charges, 1):
            body.insert(tds + n, sac(indicator, amount))
        return x12.render(x12.wrap([x12.message(code, "0001", body)], sender,
                                   "MOCKEDI", control, control.lstrip("0"), "IN"))

    def alc(indicator, amount):
        group = [seg("ALC", indicator, ["", "FC"])]
        if amount is not None:
            group.append(seg("MOA", ["8", amount]))
        return group
    lin = [i for i, item in enumerate(body) if item.tag == "LIN"][0]
    after_line = next((i for i, item in enumerate(body) if i > lin
                       and item.tag in ("LIN", "UNS")), len(body))
    for indicator, amount in reversed(list(line_charges)):
        body[after_line:after_line] = alc(indicator, amount)
    for indicator, amount in reversed(list(charges)):
        body[lin:lin] = alc(indicator, amount)
    return edifact.render(edifact.wrap([edifact.message(code, "1", body, "D:96A:UN")],
                                       sender, "MOCKEDI", control))


class Charges(MatchCase):
    """1250.00 of goods, shipped and confirmed as ordered."""

    def setUp(self):
        super().setUp()
        self.confirmed()
        self.shipped()

    def billed_with(self, total, **kw):
        summary = self.deliver(invoice(self.dialect, self.partner, total, **kw))
        # The dictionary knows ALC and SAC wherever they were put.
        self.assertEqual(summary["transactionSets"][0]["findings"], [])
        return summary

    def test_a_freight_charge_in_the_total_is_not_a_disagreement(self):
        summary = self.billed_with("1300.00", charges=[("C", "50.00")])
        self.assertEqual(summary["disagreements"], [])

    def test_nor_an_allowance_taken_off_it(self):
        summary = self.billed_with("1225.00", charges=[("A", "25.00")])
        self.assertEqual(summary["disagreements"], [])

    def test_charges_tax_and_allowances_together(self):
        summary = self.billed_with("1335.00", charges=[("C", "50.00"), ("A", "15.00")],
                                   tax="50.00")
        self.assertEqual(summary["disagreements"], [])

    def test_a_total_that_leaves_the_charge_out_is_named(self):
        summary = self.billed_with("1250.00", charges=[("C", "50.00")])
        self.assertEqual([(d["rule"], d["expected"], d["found"])
                          for d in summary["disagreements"]],
                         [("total-not-lines", "1300.00", "1250.00")])
        self.assertIn("charges 50.00", summary["disagreements"][0]["note"])

    def test_a_percentage_with_no_amount_leaves_the_total_unjudged(self):
        summary = self.billed_with("9999.00", charges=[("A", None)])
        self.assertEqual(summary["disagreements"], [])


class ChargesInEdifact(Charges):
    dialect = "EDIFACT"

    def test_the_line_items_total_is_still_checked(self):
        summary = self.billed_with("1300.00", charges=[("C", "50.00")],
                                   subtotal="1300.00")
        self.assertEqual([(d["rule"], d["expected"], d["found"])
                          for d in summary["disagreements"]],
                         [("total-not-lines", "1250.00", "1300.00")])


class LineCharges(MatchCase):
    """An allowance on one line: an X12 line's amount is worked out with it."""

    def setUp(self):
        super().setUp()
        self.confirmed()
        self.shipped()

    def test_x12_line_allowance_reaches_the_lines(self):
        summary = self.deliver(invoice("X12", self.partner, "1200.00",
                                       line_charges=[("A", "50.00")]))
        self.assertEqual(summary["disagreements"], [])


class WhatTheReadersMakeOfThem(unittest.TestCase):
    def read(self, dialect, **kw):
        interchange = parse(invoice(dialect, "NORTHWIND", "1300.00", **kw))
        message = next(interchange.messages())[1]
        return transactions.read_invoice(message, dialect)

    def test_x12(self):
        read = self.read("X12", charges=[("C", "50.00"), ("A", "10.00")],
                         line_charges=[("A", "5.00")])
        self.assertEqual(read.charges, Decimal("40.00"))
        self.assertEqual(read.lines[0].adjustment, Decimal("-5.00"))
        self.assertEqual(read.lines[0].amount, Decimal("1245.00"))
        self.assertTrue(read.charges_known)
        # TDS02 stays what it was, and is not taken for the lines' total.
        self.assertIsNone(read.line_items_total)

    def test_edifact(self):
        read = self.read("EDIFACT", charges=[("C", "50.00")])
        self.assertEqual(read.charges, Decimal("50.00"))
        self.assertEqual(read.line_items_total, Decimal(GOODS))
        self.assertTrue(read.total_stated)

    def test_an_amount_given_only_as_a_percentage(self):
        for dialect in ("X12", "EDIFACT"):
            self.assertFalse(self.read(dialect, charges=[("A", None)]).charges_known,
                             dialect)


if __name__ == "__main__":
    unittest.main(verbosity=2)
