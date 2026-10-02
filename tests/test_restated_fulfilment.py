"""An order sent again while still only received is packed and advised once (#200).

The second 850 replaces the first, and is promised a despatch and an invoice
of its own. The pair the first was promised used to stay waiting beside them:
the first despatch packed, the second found nothing left to pack and advised
the same shipment again, and the partner got one 856 twice.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import documents
from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order

HOUR = {"despatch_delay_ms": 3600000, "invoice_delay_ms": 3600000}
FEWER = (("WIDGET-001", 50, "12.50"),)
EDIFACT = {"Content-Type": "application/edifact"}


class _SentTwice(MockServerCase):
    partner = ACME
    despatch, invoice = "856", "810"

    def place(self, po_number, lines=None):
        kwargs = {"lines": lines} if lines else {}
        return self.send(x12_order(po_number, **kwargs))

    def waiting(self, query=""):
        _s, _h, rows = self.get("/_mock/scheduled" + query)
        return rows

    def sent(self, po_number):
        _s, _h, rows = self.get("/_mock/documents?direction=out&reference="
                                + po_number)
        return sorted(row["code"] for row in rows
                      if row["code"] in (self.despatch, self.invoice))


class RestatedBeforeItIsPacked(_SentTwice):
    config_kwargs = HOUR

    def setUp(self):
        super().setUp()
        self.place("PO-TWICE")
        self.summary = self.place("PO-TWICE", FEWER)

    def test_the_second_is_taken_as_the_order(self):
        self.assertEqual(self.summary["orders"], ["PO-TWICE"])
        self.assertEqual(self.order("PO-TWICE")["lines"][0]["quantity"], "50")

    def test_one_despatch_and_one_invoice_are_waiting(self):
        self.assertEqual(sorted(row["kind"] for row in self.waiting()),
                         ["despatch", "invoice"])

    def test_what_the_first_was_promised_is_withdrawn_and_says_why(self):
        withdrawn = [row for row in self.waiting("?all") if row["done_at"]]
        self.assertEqual(sorted(row["kind"] for row in withdrawn),
                         ["despatch", "invoice"])
        self.assertEqual({row["note"] for row in withdrawn}, {"order restated"})

    def test_the_shipment_is_advised_once(self):
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent("PO-TWICE"), sorted([self.despatch,
                                                         self.invoice]))
        order = self.order("PO-TWICE")
        self.assertEqual(len(order["shipments"]), 1)
        self.assertEqual(len(order["invoices"]), 1)
        self.assertEqual([row["shipped"] for row in order["lines"]], ["50"])

    def test_another_order_of_the_partners_keeps_its_promises(self):
        self.place("PO-OTHER")
        self.place("PO-TWICE", FEWER)
        self.assertEqual(sorted((row["po_number"], row["kind"])
                                for row in self.waiting()),
                         [("PO-OTHER", "despatch"), ("PO-OTHER", "invoice"),
                          ("PO-TWICE", "despatch"), ("PO-TWICE", "invoice")])


class AnOrdersRestatedBeforeItIsPacked(RestatedBeforeItIsPacked):
    partner = EURODIS
    despatch, invoice = "DESADV", "INVOIC"

    def place(self, po_number, lines=None):
        kwargs = {"lines": lines} if lines else {}
        return self.send(edifact_order(po_number, **kwargs), headers=EDIFACT)


class SentAgainWithNoDelays(_SentTwice):
    """The first is shipped and invoiced before the second arrives."""

    def test_the_second_is_refused_and_nothing_more_is_sent(self):
        self.place("PO-TWICE")
        summary = self.place("PO-TWICE", FEWER)
        self.assertEqual(summary["orders"], [])
        self.assertEqual([r["reason"] for r in summary["refusals"]],
                         [documents.NUMBER_IN_USE])
        self.assertEqual(self.waiting(), [])
        self.assertEqual(self.sent("PO-TWICE"), ["810", "856"])


class SentAgainWithNoDelaysAndDuplicatesAllowed(_SentTwice):
    config_kwargs = {"allow_duplicates": True}

    def test_each_shipment_is_advised_once(self):
        self.place("PO-TWICE")
        self.place("PO-TWICE", FEWER)
        self.assertEqual(self.waiting(), [])
        self.assertEqual(self.sent("PO-TWICE"), ["810", "810", "856", "856"])
        self.assertEqual(len(self.order("PO-TWICE")["shipments"]), 2)


del _SentTwice

if __name__ == "__main__":
    unittest.main()
