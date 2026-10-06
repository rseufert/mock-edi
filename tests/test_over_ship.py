"""A seller that ships more than it confirmed: the `over-ship` behaviour.

The buying side has always known the case - a ship notice for more than was
confirmed, or more than was ordered, is a disagreement it reports - and no
seller the mock could play ever produced one. So a film of it needed a script
standing in for the seller.

The acknowledgment confirms what was ordered. The ship notice carries three
in ten more on every line, and the invoice bills what shipped: the warehouse
packed a full carton, and the buyer finds out from the 856 and at the dock,
not from a promise (#212).
"""
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import documents
from mockedi.testing import Mock

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order

LINES = [("WIDGET-001", 100, "12.50"), ("BRKT-050", 10, "4.15")]


class HowMuchMore(unittest.TestCase):

    def test_three_in_ten_rounded_up_and_never_less_than_one_extra(self):
        for confirmed, packed in (("100", "130"), ("10", "13"), ("1", "2"),
                                  ("3", "4"), ("7", "10"), ("24", "32")):
            with self.subTest(confirmed=confirmed):
                self.assertEqual(documents.over_shipped(Decimal(confirmed)),
                                 Decimal(packed))

    def test_a_fraction_of_a_unit_gets_one_whole_unit_more_and_stays_a_fraction(self):
        # Half a kilogram ordered is a kilogram and a half shipped: the one
        # extra unit applies, and nothing is rounded that was not asked to be.
        self.assertEqual(documents.over_shipped(Decimal("0.5")), Decimal("1.5"))
        self.assertEqual(documents.over_shipped(Decimal("2.5")), Decimal("4"))


class InX12(MockServerCase):

    def setUp(self):
        super().setUp()
        self.patch("/_mock/partners/" + ACME, {"behaviour": "over-ship"})
        self.send(x12_order("OVER-X12", lines=LINES))

    def quantities(self, kind, tag, at):
        message = self.document(ACME, kind).groups[0].messages[0]
        return [item.get(at) for item in message.segments if item.tag == tag]

    def test_the_855_confirms_what_was_ordered(self):
        message = self.document(ACME, "response").groups[0].messages[0]
        acks = [item for item in message.segments if item.tag == "ACK"]
        self.assertEqual([(item.get(1), item.get(2)) for item in acks],
                         [("IA", "100"), ("IA", "10")])
        self.assertEqual(message.find("BAK").get(2), "AD")

    def test_the_856_ships_more_on_every_line(self):
        self.assertEqual(self.quantities("despatch", "SN1", 2), ["130", "13"])

    def test_and_says_what_was_ordered_beside_it(self):
        self.assertEqual(self.quantities("despatch", "SN1", 5), ["100", "10"])

    def test_the_810_bills_what_shipped(self):
        self.assertEqual(self.quantities("invoice", "IT1", 2), ["130", "13"])
        message = self.document(ACME, "invoice").groups[0].messages[0]
        # 130 x 12.50 + 13 x 4.15
        self.assertEqual(message.find("TDS").get(1), "167895")

    def test_the_sellers_view_of_the_order_says_all_three(self):
        _status, _headers, order = self.get("/_mock/orders/OVER-X12")
        self.assertEqual(
            [(line["quantity"], line["confirmed"], line["shipped"], line["invoiced"])
             for line in order["lines"]],
            [("100", "100", "130", "130"), ("10", "10", "13", "13")])

    def test_every_document_it_wrote_is_clean_by_its_own_dictionary(self):
        for kind in ("response", "despatch", "invoice"):
            with self.subTest(kind=kind):
                payload = self.mailbox(ACME, kind)[0]["payload"]
                said = self.mock.findings(payload)
                self.assertEqual(len(said), 1, said)
                self.assertTrue(said[0].endswith(": accepted"), said)


class InEdifact(MockServerCase):

    def setUp(self):
        super().setUp()
        self.patch("/_mock/partners/" + EURODIS, {"behaviour": "over-ship"})
        self.send(edifact_order("OVER-EDI", lines=[("PANEL-A4", 10, "89.00")]))

    def quantity(self, kind, qualifier):
        message = next(self.document(EURODIS, kind).messages())[1]
        return [item.comp(1, 2) for item in message.segments
                if item.tag == "QTY" and item.comp(1, 1) == qualifier]

    def test_the_ordrsp_confirms_what_was_ordered(self):
        self.assertEqual(self.quantity("response", "21"), ["10"])

    def test_the_desadv_despatches_more(self):
        self.assertEqual(self.quantity("despatch", "12"), ["13"])

    def test_the_invoic_bills_what_was_despatched(self):
        self.assertEqual(self.quantity("invoice", "47"), ["13"])


class ItIsASellersBehaviour(MockServerCase):

    def test_it_is_listed_with_what_it_does(self):
        _status, _headers, behaviours = self.get("/_mock/behaviours")
        self.assertIn("ship three in ten more", behaviours["over-ship"])

    def test_a_supplier_cannot_be_given_it(self):
        self.post("/_mock/partners", {"id": "SELLCO", "name": "Sell Co",
                                      "role": "supplier"})
        status, _headers, data = self.patch(
            "/_mock/partners/SELLCO", {"behaviour": "over-ship"})
        self.assertEqual(status, 400, data)

    def test_another_partner_is_shipped_what_it_ordered(self):
        self.send(x12_order("OVER-NOT", lines=LINES))
        message = self.document(ACME, "despatch").groups[0].messages[0]
        self.assertEqual([item.get(2) for item in message.segments
                          if item.tag == "SN1"], ["100", "10"])


class WhatTheBuyerMakesOfIt(unittest.TestCase):
    """Two mocks: one over-ships, and the other says so."""

    def setUp(self):
        self.seller = Mock.start(as2_id="SELLCO")
        self.buyer = Mock.start(as2_id="BUYCO")
        self.addCleanup(self.seller.close)
        self.addCleanup(self.buyer.close)
        self.seller.expect("POST", "/_mock/partners", {
            "id": "BUYCO", "name": "Buy Co", "behaviour": "over-ship",
            "as2_url": self.buyer.base + "/edi"}, status=201)
        self.buyer.expect("POST", "/_mock/partners", {
            "id": "SELLCO", "name": "Sell Co", "role": "supplier",
            "as2_url": self.seller.base + "/edi"}, status=201)
        self.buyer.expect("POST", "/_mock/purchase", {
            "partner": "SELLCO", "po_number": "OVER-PAIR",
            "lines": [{"sku": "WIDGET-001", "quantity": "100", "uom": "EA",
                       "price": "12.50"}]}, status=201)
        self.buyer.exchange(self.seller)
        self.found = self.buyer.expect(
            "GET", "/_mock/disagreements?po=OVER-PAIR")

    def rules(self, code):
        return sorted(row["rule"] for row in self.found if row["code"] == code)

    def test_the_855_draws_nothing(self):
        self.assertEqual(self.rules("855"), [])

    def test_the_856_is_more_than_confirmed_and_more_than_ordered(self):
        self.assertEqual(self.rules("856"), ["shipped-more-than-confirmed",
                                             "shipped-more-than-ordered"])
        by_rule = {row["rule"]: row for row in self.found if row["code"] == "856"}
        self.assertEqual((by_rule["shipped-more-than-confirmed"]["expected"],
                          by_rule["shipped-more-than-confirmed"]["found"]),
                         ("100", "130"))
        self.assertEqual((by_rule["shipped-more-than-ordered"]["expected"],
                          by_rule["shipped-more-than-ordered"]["found"]),
                         ("100", "130"))

    def test_the_810_bills_what_shipped_and_is_not_billed_more_than_shipped(self):
        self.assertNotIn("billed-more-than-shipped", self.rules("810"))

    def test_every_set_is_still_accepted(self):
        # A disagreement is not a syntax error: the 997s say accepted.
        sets = self.seller.documents(direction="in", code="997", limit=20)
        self.assertTrue(sets)
        self.assertTrue(all(row["accepted"] for row in sets))

    def test_the_buyers_view_of_the_order_has_the_four_numbers(self):
        line = self.buyer.order("OVER-PAIR")["lines"][0]
        self.assertEqual((line["quantity"], line["confirmed"], line["shipped"],
                          line["invoiced"]), ("100", "100", "130", "130"))


if __name__ == "__main__":
    unittest.main()
