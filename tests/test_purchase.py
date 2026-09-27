"""Orders the mock places with a supplier, as they are stored.

The mock has only ever received orders, and every column of `purchase_order`
and `order_line` meant what the seller did. An order it places means what it
asked for, and the seller's machinery - deciding lines, packing, invoicing,
applying a customer's change - must never touch one. These are the storage
rules; placing one over HTTP and reading what the supplier sends back is
#125's wiring, tested with it.
"""
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import db, documents, schema
from mockedi.transactions import Party

from support import ACME, MockServerCase, x12_change, x12_order

US = Party(role="BY", name="Mock EDI Inc", identifier="MOCKEDI",
           street="1 Test St", city="Springfield", region="IL", postal="62701",
           country="US")
SUPPLIER = {"id": "NORTHWIND", "name": "Northwind Traders", "street": "",
            "city": "", "region": "", "postal": "", "country": "US"}
LINES = [{"sku": "WIDGET-001", "quantity": "100", "uom": "EA", "price": "12.50"},
         {"sku": "GADGET-042", "quantity": "5", "price": "3.10"}]


class Stored(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        db.seed(self.conn)
        self.addCleanup(self.conn.close)

    def place(self, **request):
        request.setdefault("lines", LINES)
        return documents.place_order(self.conn, SUPPLIER, US, request)


class PlacingAnOrder(Stored):
    def test_it_is_stored_as_placed_with_what_was_asked_for(self):
        order = self.place(requested_on="2026-10-15")
        self.assertEqual((order["direction"], order["status"], order["partner"]),
                         ("placed", "placed", "NORTHWIND"))
        self.assertEqual(order["total"], "1265.50")      # 100 x 12.50 + 5 x 3.10
        self.assertEqual(order["ship_to_id"], "MOCKEDI")
        line = documents.order_lines(self.conn, order["po_number"])[0]
        self.assertEqual((line["quantity"], line["price"], line["ordered_price"]),
                         ("100", "12.50", "12.50"))
        # Nothing the supplier has said yet, and nothing the seller decided.
        self.assertEqual((line["status"], line["confirmed"], line["shipped"],
                          line["invoiced"]), ("", "0", "0", "0"))

    def test_the_number_is_the_mocks_own_unless_given(self):
        first, second = self.place(), self.place()
        self.assertEqual(first["po_number"], "4800000001")
        self.assertEqual(second["po_number"], "4800000002")
        self.assertEqual(self.place(po_number="PO-MINE")["po_number"], "PO-MINE")

    def test_the_catalogue_fills_in_the_other_item_number(self):
        order = self.place()
        line = documents.order_lines(self.conn, order["po_number"])[0]
        self.assertTrue(line["upc"])
        self.assertTrue(line["description"])

    def test_every_problem_is_named_at_once(self):
        self.place(po_number="PO-TAKEN")
        with self.assertRaises(documents.Refused) as caught:
            self.place(po_number="PO-TAKEN", requested_on="15/10/2026",
                       lines=[{"quantity": "0", "price": "x"}])
        text = str(caught.exception)
        for fragment in ("already exists", "not a date", "names no item",
                         "price 'x' is not a number", "for 0 of something"):
            self.assertIn(fragment, text)
        self.assertEqual(len(caught.exception.problems), 5)

    def test_it_reads_as_the_order_an_850_carries(self):
        order = documents.as_order(self.conn, self.place()["po_number"], US, SUPPLIER)
        self.assertEqual(order.parties["BY"].identifier, "MOCKEDI")
        self.assertEqual(order.parties["SE"].identifier, "NORTHWIND")
        self.assertEqual([(l.sku, l.quantity) for l in order.lines],
                         [("WIDGET-001", Decimal("100")), ("GADGET-042", Decimal("5"))])
        self.assertEqual(order.total, Decimal("1265.50"))


class ChangingIt(Stored):
    def setUp(self):
        super().setUp()
        self.po = self.place()["po_number"]

    def change(self, **request):
        return documents.change_placed(self.conn, self.po, request)

    def test_a_line_changed_added_and_deleted(self):
        change = self.change(lines=[
            {"line": "1", "quantity": "80"},
            {"line": "2", "action": "delete"},
            {"action": "add", "sku": "WIDGET-001", "quantity": "1", "price": "12.00"}])
        self.assertEqual([(l.number, l.action, l.quantity) for l in change.lines],
                         [("1", "CA", Decimal("80")), ("2", "DI", Decimal("5")),
                          ("3", "AI", Decimal("1"))])
        self.assertFalse(change.cancels)
        rows = {r["line"]: r for r in documents.order_lines(self.conn, self.po)}
        self.assertEqual(sorted(rows), ["1", "3"])
        self.assertEqual((rows["1"]["quantity"], rows["1"]["price"]), ("80", "12.50"))
        self.assertEqual(documents.order_row(self.conn, self.po)["total"], "1012.00")

    def test_changes_are_numbered_in_sequence(self):
        first = self.change(lines=[{"line": "1", "quantity": "90"}])
        second = self.change(lines=[{"line": "1", "quantity": "95"}])
        self.assertEqual((first.sequence, second.sequence), ("1", "2"))

    def test_a_cancellation(self):
        change = self.change(cancel=True)
        self.assertTrue(change.cancels)
        self.assertEqual(documents.order_row(self.conn, self.po)["status"], "cancelled")
        with self.assertRaises(documents.Refused):
            self.change(lines=[{"line": "1", "quantity": "1"}])

    def test_what_cannot_be_changed_is_named(self):
        with self.assertRaises(documents.Refused) as caught:
            self.change(lines=[{"line": "9", "quantity": "1"},
                               {"line": "1", "action": "replace"},
                               {"line": "1", "action": "add", "sku": "WIDGET-001",
                                "quantity": "1", "price": "1"}])
        self.assertEqual(len(caught.exception.problems), 3, caught.exception.problems)

    def test_deleting_every_line_is_a_cancellation_not_a_change(self):
        with self.assertRaises(documents.Refused) as caught:
            self.change(lines=[{"line": "1", "action": "delete"},
                               {"line": "2", "action": "delete"}])
        self.assertIn("cancel: true", str(caught.exception))

    def test_only_a_placed_order_is_changed_this_way(self):
        with self.assertRaises(LookupError):
            documents.change_placed(self.conn, "NO-SUCH-PO", {"cancel": True})


class TheSellerLeavesItAlone(MockServerCase):
    """A placed order's number, arriving from a customer, is not theirs."""

    def setUp(self):
        super().setUp()
        conn = self.httpd.mock.conn
        documents.place_order(conn, SUPPLIER, US,
                              {"po_number": "PO-PLACED", "lines": LINES})

    def test_a_customers_850_cannot_replace_it(self):
        summary = self.send(x12_order("PO-PLACED"))
        self.assertEqual(summary["orders"], [])
        self.assertEqual(self.order("PO-PLACED")["direction"], "placed")
        self.assertEqual(self.order("PO-PLACED")["partner"], "NORTHWIND")

    def test_nor_can_an_860_change_it(self):
        summary = self.send(x12_change("PO-PLACED"))
        self.assertEqual([r["reason"] for r in summary["refusals"]],
                         [documents.NOT_FOUND])
        self.assertEqual(self.order("PO-PLACED")["lines"][0]["quantity"], "100")

    def test_nor_an_850_restating_it(self):
        summary = self.send(x12_order("PO-PLACED", purpose="04"))  # a restatement
        self.assertIn("this mock placed", summary["refusals"][0]["reason"])
        self.assertEqual(self.order("PO-PLACED")["lines"][0]["quantity"], "100")

    def test_and_the_mock_does_not_answer_it_on_demand(self):
        status, _h, data = self.post("/_mock/send", {
            "partner": ACME, "kind": schema.RESPONSE, "po": "PO-PLACED"})
        self.assertEqual(status, 400, data)
        self.assertIn("the mock placed", data["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
