"""A unit price is carried as it was given, and only amounts are rounded (#206).

`POST /_mock/purchase` at 0.125 each used to store and send 0.12. A thousand
of them was then an order for 120.00 that said 125.00, and when the supplier
confirmed at the real price the mock reported a disagreement between 0.12
and 0.12.

`PO104`, `POC06` and `IT104` are X12 element 212, and `PRI`'s is EDIFACT
5118: decimals, not money with two places.
"""
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import money, schema

from support import ACME, MockServerCase, parse, x12_order
from test_reconciliation import answer, line

NORTHWIND = "NORTHWIND"
THOUSAND = [{"sku": "WIDGET-001", "quantity": "1000", "uom": "EA",
             "price": "0.125"}]


class TheText(unittest.TestCase):
    def test_every_decimal_given_is_kept(self):
        for value, expected in (("0.125", "0.125"), ("12.3456", "12.3456"),
                                ("0.0005", "0.0005")):
            with self.subTest(value=value):
                self.assertEqual(money.unit_price(value), expected)

    def test_and_there_are_always_at_least_two(self):
        for value, expected in (("12.5", "12.50"), ("3", "3.00"),
                                ("100", "100.00"), ("12.50", "12.50"),
                                ("0", "0.00")):
            with self.subTest(value=value):
                self.assertEqual(money.unit_price(value), expected)

    def test_zeros_past_the_second_place_are_not_precision(self):
        self.assertEqual(money.unit_price("0.1250"), "0.125")
        self.assertEqual(money.unit_price(Decimal("12.5000")), "12.50")

    def test_it_is_never_written_with_an_exponent(self):
        self.assertEqual(money.unit_price(Decimal("1E+2")), "100.00")
        self.assertEqual(money.unit_price(Decimal("1E-7")), "0.0000001")


class BuyingCase(MockServerCase):
    def placed(self, partner=NORTHWIND, **request):
        request.setdefault("lines", THOUSAND)
        status, _h, data = self.post("/_mock/purchase",
                                     dict(request, partner=partner))
        self.assertEqual(status, 201, data)
        return data

    def segments(self, partner, kind, tag):
        payload = self.mailbox(partner, kind, leave=True)[-1]["payload"]
        message = parse(payload).groups[0].messages[0]
        return [item for item in message.segments if item.tag == tag]


class AnOrderAtAThreeDecimalPrice(BuyingCase):
    def test_it_is_stored_as_given_and_totalled_from_that(self):
        order = self.placed(po_number="PO-MILLE")
        self.assertEqual(order["lines"][0]["price"], "0.125")
        self.assertEqual(order["lines"][0]["ordered_price"], "0.125")
        # A thousand at 0.125, not a thousand at 0.12.
        self.assertEqual(order["total"], "125.00")

    def test_the_850_says_it(self):
        self.placed(po_number="PO-MILLE")
        po1 = self.segments(NORTHWIND, schema.ORDER, "PO1")[0]
        self.assertEqual(po1.elements[:4], ["1", "1000", "EA", "0.125"])

    def test_so_does_an_orders(self):
        self.post("/_mock/partners", {"id": "NORDIC", "dialect": "EDIFACT",
                                      "role": "supplier"})
        self.placed(partner="NORDIC", po_number="PO-MILLE-EU")
        pri = self.segments("NORDIC", schema.ORDER, "PRI")[0]
        self.assertEqual(pri.comp(1, 2), "0.125")

    def test_a_change_of_price_is_sent_as_given(self):
        self.placed(po_number="PO-MILLE")
        status, _h, data = self.post("/_mock/purchase/PO-MILLE/change", {
            "lines": [{"line": "1", "price": "0.1275"}]})
        self.assertEqual(status, 200, data)
        self.assertEqual(self.segments(NORTHWIND, schema.CHANGE, "POC")[0].get(6),
                         "0.1275")
        self.assertEqual(data["lines"][0]["price"], "0.1275")
        self.assertEqual(data["total"], "127.50")

    def test_a_price_the_element_cannot_carry_is_refused(self):
        # Kept as given only as far as PO104 can hold it: 17 characters.
        status, _h, data = self.post("/_mock/purchase", {
            "partner": NORTHWIND, "lines": [dict(
                THOUSAND[0], price="0.1234567890123456789")]})
        self.assertEqual(status, 400, data)
        self.assertTrue([p for p in data["problems"]
                         if p.startswith("line 1: price") and "PO104" in p],
                        data["problems"])

    def test_an_ordinary_price_reads_as_it_always_did(self):
        order = self.placed(po_number="PO-PLAIN", lines=[
            {"sku": "WIDGET-001", "quantity": "10", "price": "12.5"}])
        self.assertEqual(order["lines"][0]["price"], "12.50")
        self.assertEqual(
            self.segments(NORTHWIND, schema.ORDER, "PO1")[0].get(4), "12.50")


class TheSupplierAnswersAtThatPrice(BuyingCase):
    def says(self, price):
        self.placed(po_number="PO-MILLE")
        return self.send(answer("PO-MILLE", [
            line("1", "WIDGET-001", 1000, price=price)]))

    def test_the_same_price_is_no_disagreement(self):
        self.assertEqual(self.says("0.125")["disagreements"], [])

    def test_a_different_one_is_reported_with_both_as_given(self):
        found = self.says("0.13")["disagreements"]
        self.assertEqual([(d["rule"], d["expected"], d["found"]) for d in found],
                         [("price-differs", "0.125", "0.13")])

    def test_a_price_a_fraction_of_a_cent_out_is_still_a_difference(self):
        # Rounded to cents these were the same number.
        found = self.says("0.124")["disagreements"]
        self.assertEqual([(d["expected"], d["found"]) for d in found],
                         [("0.125", "0.124")])


class SellingAtACataloguePrice(MockServerCase):
    """The other side: a buyer's 850 quoting a price finer than the cent."""

    def test_the_price_ordered_is_kept_and_quoted_back_exactly(self):
        self.send(x12_order("PO-FINE", lines=(("WIDGET-001", 10, "12.495"),)))
        stored = self.get("/_mock/orders/PO-FINE")[2]["lines"][0]
        self.assertEqual(stored["ordered_price"], "12.495")
        self.assertEqual(stored["price"], "12.50")
        self.assertEqual(stored["status"], "IP")
        self.assertIn("the order said 12.495", stored["reason"])


if __name__ == "__main__":
    unittest.main()
