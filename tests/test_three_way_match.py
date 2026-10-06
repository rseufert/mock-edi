"""The buyer's three-way match: an invoice against the order, the 855 and the 856.

The six rules for an 810 or an INVOIC (#152), each one comparison naming both
numbers, in both dialects; the match failing visibly in each; and a real
seller's `duplicate-invoice`, `out-of-order` and `no-invoice`, as a buyer mock
reads them. Like every disagreement, none of these reaches the 997.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, schema, transactions, x12
from mockedi.transactions import Party

from support import MockServerCase, _next_control, parse
from test_reconciliation import AS_ORDERED, ORDERED, answer, line
from test_two_mocks import Pair

NORTHWIND = "NORTHWIND"
NORDIC = "NORDIC"
WHEN = datetime.datetime(2026, 9, 28, 12, 0)


def document(kind, po_number, rows, invoice=None, sender=NORTHWIND, dialect="X12"):
    """A supplier's 856 or 810, written by the mock's own seller-side writer."""
    supplier = Party(role="SE", name="Supplier", identifier=sender, country="US")
    mock = {"id": "MOCKEDI", "name": "Mock EDI", "qualifier": "ZZ", "street": "",
            "city": "", "region": "", "postal": "", "country": "US", "duns": "",
            "dialect": dialect, "version": "004010"}
    order = {"po_number": po_number, "seller_order": "SO-1", "ordered_on":
             "2026-09-27", "currency": "USD", "ship_to_name": "Mock EDI",
             "ship_to_id": "MOCKEDI"}
    shipment = {"shipment_id": "SH-%s" % _next_control(4), "shipped_on": "2026-09-28",
                "tracking": "1Z1", "bol": "BOL-1", "carrier": "UPS", "scac": "UPSN",
                "cartons": "1", "weight": "10"}
    if kind == schema.DESPATCH:
        body = transactions.write_despatch(dialect, supplier, mock, order, rows,
                                           shipment, WHEN)
    else:
        body = transactions.write_invoice(dialect, supplier, mock, order, rows,
                                          invoice, shipment, WHEN)
    code = schema.set_code(dialect, kind)
    control = _next_control(9)
    if dialect == "X12":
        return x12.render(x12.wrap([x12.message(code, "0001", body)], sender,
                                   "MOCKEDI", control, control.lstrip("0"),
                                   schema.lookup(dialect, code).group))
    return edifact.render(edifact.wrap([edifact.message(code, "1", body, "D:96A:UN")],
                                       sender, "MOCKEDI", control))


def item(number, sku, quantity, price="12.50"):
    return {"line": number, "sku": sku, "upc": "", "description": "Item",
            "quantity": str(quantity), "uom": "EA", "price": price,
            "shipped": str(quantity), "invoiced": str(quantity)}


def bill(number, rows, total=None, subtotal=None, tax="0.00"):
    lines = sum(float(r["invoiced"]) * float(r["price"]) for r in rows)
    return {"invoice_number": number, "invoiced_on": "2026-09-28",
            "currency": "USD", "tax": tax,
            "subtotal": subtotal if subtotal is not None else "%.2f" % lines,
            "total": total if total is not None else "%.2f" % (lines + float(tax))}


LINE_1 = [item("1", "WIDGET-001", 100)]


class MatchCase(MockServerCase):
    dialect = "X12"

    def setUp(self):
        super().setUp()
        self.partner = NORTHWIND
        if self.dialect == "EDIFACT":
            self.partner = NORDIC
            self.post("/_mock/partners", {"id": NORDIC, "dialect": "EDIFACT",
                                          "version": "D:96A:UN", "role": "supplier",
                                          "qualifier": "14"})
        status, _h, data = self.post("/_mock/purchase", {
            "partner": self.partner, "po_number": "PO-B", "lines": ORDERED})
        self.assertEqual(status, 201, data)

    def deliver(self, payload):
        headers = ({"Content-Type": "application/edifact"}
                   if self.dialect == "EDIFACT" else None)
        summary = self.send(payload, headers=headers)
        self.assertTrue(summary["accepted"], summary["transactionSets"])
        return summary

    def confirmed(self, lines=AS_ORDERED):
        return self.deliver(answer("PO-B", lines, sender=self.partner,
                                   dialect=self.dialect))

    def shipped(self, rows=LINE_1):
        return self.deliver(document(schema.DESPATCH, "PO-B", rows,
                                     sender=self.partner, dialect=self.dialect))

    def billed(self, rows=LINE_1, number="INV-1", **totals):
        return self.deliver(document(schema.INVOICE, "PO-B", rows,
                                     bill(number, rows, **totals),
                                     sender=self.partner, dialect=self.dialect))

    def rules(self, summary):
        return [(d["rule"], d["line"], d["expected"], d["found"])
                for d in summary["disagreements"]]


class TheRules(MatchCase):
    def test_an_invoice_that_matches_says_nothing(self):
        self.confirmed()
        self.shipped()
        self.assertEqual(self.billed()["disagreements"], [])
        line_1 = self.order("PO-B")["reconciliation"][0]
        self.assertEqual((line_1["confirmed"], line_1["shipped"], line_1["billed"]),
                         ("100", "100", "100"))

    def test_billed_before_shipped(self):
        self.confirmed()
        summary = self.billed()
        self.assertEqual([r[0] for r in self.rules(summary)],
                         ["billed-before-shipped"])
        self.assertIn("before any 856", summary["disagreements"][0]["note"])

    def test_billed_more_than_shipped(self):
        self.confirmed()
        self.shipped([item("1", "WIDGET-001", 60)])
        summary = self.billed()
        self.assertEqual(self.rules(summary),
                         [("billed-more-than-shipped", "1", "60", "100")])

    def test_a_second_bill_counts_what_the_first_billed(self):
        self.confirmed()
        self.shipped()
        self.billed([item("1", "WIDGET-001", 70)], number="INV-1")
        summary = self.billed([item("1", "WIDGET-001", 40)], number="INV-2")
        # 110 billed, against 100 shipped and against 100 ordered: two
        # facts, and both are said (#309).
        self.assertEqual(self.rules(summary),
                         [("billed-more-than-shipped", "1", "100", "110"),
                          ("billed-more-than-ordered", "1", "100", "110")])

    def test_billed_more_than_ordered_when_the_despatch_covered_it(self):
        """The gap in #309: 130 shipped, 130 billed, 100 ordered. The bill is
        no more than shipped, and used to draw nothing."""
        self.confirmed()
        self.shipped([item("1", "WIDGET-001", 130)])
        summary = self.billed([item("1", "WIDGET-001", 130)])
        self.assertEqual(self.rules(summary),
                         [("billed-more-than-ordered", "1", "100", "130")])
        self.assertIn("ordered 100, 130 billed",
                      summary["disagreements"][0]["note"])
        self.assertIn("INV-1", summary["disagreements"][0]["note"])

    def test_billed_more_than_ordered_with_no_despatch_at_all(self):
        """It does not wait for an 856: what was ordered is known already."""
        self.confirmed()
        summary = self.billed([item("1", "WIDGET-001", 130)])
        self.assertEqual(self.rules(summary),
                         [("billed-before-shipped", "", "an 856", "none"),
                          ("billed-more-than-ordered", "1", "100", "130")])

    def test_a_confirmation_of_more_does_not_raise_what_was_ordered(self):
        self.deliver(answer("PO-B", [line("1", "WIDGET-001", 130),
                                     line("2", "BRKT-050", 40, price="4.15")],
                            sender=self.partner, dialect=self.dialect))
        self.shipped([item("1", "WIDGET-001", 130)])
        summary = self.billed([item("1", "WIDGET-001", 130)])
        self.assertEqual(self.rules(summary),
                         [("billed-more-than-ordered", "1", "100", "130")])

    def test_but_a_quantity_the_buyer_raised_is_what_was_ordered(self):
        status, _h, data = self.post("/_mock/purchase/PO-B/change",
                                     {"lines": [{"line": "1", "quantity": "130"}]})
        self.assertEqual(status, 200, data)
        self.confirmed()
        self.shipped([item("1", "WIDGET-001", 130)])
        summary = self.billed([item("1", "WIDGET-001", 130)])
        self.assertNotIn("billed-more-than-ordered",
                         [rule[0] for rule in self.rules(summary)])

    def test_a_second_bill_within_what_shipped_can_still_pass_what_was_ordered(self):
        self.confirmed()
        self.shipped([item("1", "WIDGET-001", 130)])
        first = self.billed([item("1", "WIDGET-001", 70)], number="INV-1")
        self.assertEqual(first["disagreements"], [])
        second = self.billed([item("1", "WIDGET-001", 60)], number="INV-2")
        self.assertEqual(self.rules(second),
                         [("billed-more-than-ordered", "1", "100", "130")])

    def test_a_repeated_invoice_is_not_more_billed(self):
        self.confirmed()
        self.shipped([item("1", "WIDGET-001", 130)])
        self.billed([item("1", "WIDGET-001", 130)], number="INV-1")
        again = self.billed([item("1", "WIDGET-001", 130)], number="INV-1")
        self.assertEqual([rule[0] for rule in self.rules(again)],
                         ["invoice-repeated"])

    def test_an_invoice_number_repeated(self):
        self.confirmed()
        self.shipped()
        self.billed()
        summary = self.billed()
        # The same bill twice: said once, and counted once.
        self.assertEqual(self.rules(summary),
                         [("invoice-repeated", "", "", "INV-1")])
        self.assertEqual(self.order("PO-B")["reconciliation"][0]["billed"], "100")

    def test_a_price_neither_ordered_nor_confirmed(self):
        self.confirmed()
        self.shipped()
        summary = self.billed([item("1", "WIDGET-001", 100, price="13.10")])
        self.assertEqual(self.rules(summary),
                         [("price-not-agreed", "1", "12.50", "13.10")])

    def test_but_the_confirmed_price_is_agreed(self):
        # The 855 changed the price, and said so there; billing it is not news.
        self.confirmed([line("1", "WIDGET-001", 100, status="IP", price="13.10"),
                        AS_ORDERED[1]])
        self.shipped()
        summary = self.billed([item("1", "WIDGET-001", 100, price="13.10")])
        self.assertEqual(summary["disagreements"], [])

    def test_a_total_that_is_not_its_lines(self):
        self.confirmed()
        self.shipped()
        summary = self.billed(total="1300.00", subtotal="1300.00")
        self.assertEqual([r[0] for r in self.rules(summary)], ["total-not-lines"])
        found = summary["disagreements"][0]
        self.assertEqual((found["expected"], found["found"]), ("1250.00", "1300.00"))

    def test_tax_is_not_a_disagreement(self):
        self.confirmed()
        self.shipped()
        summary = self.billed(tax="100.00")
        self.assertEqual(summary["disagreements"], [])

    def test_billing_a_cancelled_order(self):
        self.post("/_mock/purchase/PO-B/change", {"cancel": True})
        summary = self.billed()
        self.assertIn(("billed-cancelled", "", "cancelled", "INV-1"),
                      self.rules(summary))


class TheRulesInEdifact(TheRules):
    dialect = "EDIFACT"


class TheThreeWayMatch(MatchCase):
    """Order 100 at 12.50; the 855 confirms 90; the 856 ships 90; the 810 bills
    100 at 12.75. Each document is judged against what came before it."""

    def run_match(self):
        confirmed = self.confirmed([line("1", "WIDGET-001", 90, status="IQ",
                                         ordered=100, reason="Short"),
                                    AS_ORDERED[1]])
        shipped = self.shipped([item("1", "WIDGET-001", 90)])
        billed = self.billed([item("1", "WIDGET-001", 100, price="12.75")])
        return confirmed, shipped, billed

    def test_fails_visibly(self):
        confirmed, shipped, billed = self.run_match()
        self.assertEqual(self.rules(confirmed), [("confirmed-less", "1", "100", "90")])
        self.assertEqual(shipped["disagreements"], [])        # #151's to judge
        self.assertEqual(sorted(self.rules(billed)),
                         [("billed-more-than-shipped", "1", "90", "100"),
                          ("price-not-agreed", "1", "12.50", "12.75")])
        order = self.order("PO-B")
        self.assertEqual([(r["ordered"], r["confirmed"], r["shipped"], r["billed"])
                          for r in order["reconciliation"]][0],
                         ("100", "90", "90", "100"))
        self.assertEqual(len(order["disagreements"]), 3)

    def test_and_every_document_is_acknowledged_as_accepted(self):
        for summary in self.run_match():
            self.assertTrue(summary["transactionSets"][0]["accepted"])
            self.assertEqual(summary["transactionSets"][0]["findings"], [])
        for row in self.mailbox(self.partner, "acknowledgment"):
            interchange = parse(row["payload"])
            if interchange.dialect == "X12":
                self.assertEqual(interchange.groups[0].messages[0].find("AK9").get(1),
                                 "A")


class TheThreeWayMatchInEdifact(TheThreeWayMatch):
    dialect = "EDIFACT"


class TwoMocks(unittest.TestCase):
    """A real seller misbehaving on demand, as the buyer reads it."""

    def run_flow(self, behaviour, dialect="X12"):
        pair = Pair(dialect=dialect, behaviour=behaviour)
        self.addCleanup(pair.close)
        pair.place("PO-2M")
        pair.exchange()
        return pair, pair.buyer.order("PO-2M")

    def rules(self, order):
        return sorted({d["rule"] for d in order["disagreements"]})

    def test_duplicate_invoice_is_an_invoice_number_repeated(self):
        for dialect in ("X12", "EDIFACT"):
            _pair, order = self.run_flow("duplicate-invoice", dialect)
            self.assertEqual(self.rules(order), ["invoice-repeated"], dialect)
            self.assertEqual([r["billed"] for r in order["reconciliation"]],
                             ["100", "40"], dialect)

    def test_out_of_order_is_billed_before_shipped(self):
        # This seller reverses the lot - 810, then 856, then 855 - so the
        # despatch arrives before anything was confirmed as well. That was
        # always true of the flow; #151 gave the buyer the rule to say it.
        for dialect in ("X12", "EDIFACT"):
            _pair, order = self.run_flow("out-of-order", dialect)
            self.assertEqual(self.rules(order),
                             ["billed-before-shipped", "shipped-before-confirmed"],
                             dialect)
            # Then resolved: the 856 arrives, and shipped meets billed.
            self.assertEqual([(r["shipped"], r["billed"])
                              for r in order["reconciliation"]],
                             [("100", "100"), ("40", "40")], dialect)

    def test_no_invoice_is_shipped_and_never_billed(self):
        _pair, order = self.run_flow("no-invoice")
        self.assertEqual(order["disagreements"], [])
        self.assertEqual([(r["shipped"], r["billed"])
                          for r in order["reconciliation"]],
                         [("100", "0"), ("40", "0")])

    def test_and_each_is_acknowledged_as_a_clean_flow_is(self):
        clean, _order = self.run_flow("accept")
        for behaviour in ("duplicate-invoice", "out-of-order"):
            pair, _order = self.run_flow(behaviour)
            verdicts = pair.verdicts()
            # Per document, whatever the count: a duplicate 810 is two 810s,
            # and each has to be acknowledged as the one clean 810 was.
            for code, (verdict,) in clean.verdicts().items():
                self.assertTrue(verdicts.get(code), (behaviour, code))
                for each in verdicts[code]:
                    self.assertEqual(each, verdict, (behaviour, code))


if __name__ == "__main__":
    unittest.main(verbosity=2)
