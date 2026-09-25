"""One shipment per order, whichever way round the delays are set.

The despatch and the invoice are separate pieces of scheduled work, and
either can come due first. The invoice has to pack the goods if nobody has
yet - it needs something to bill - so the despatch must not pack them a
second time when it arrives. Getting that wrong gives an order two
consignments with two bills of lading, and an 856 and an 810 that name
different ones: the cross-reference a buyer uses to match a bill to a
delivery, quietly broken, and only in the ordering nobody runs by default.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, MockServerCase, x12_order

HOUR = 3600000


class OrderingCase(MockServerCase):
    """Drives one order to completion and reports what it produced."""

    def fulfil(self, po_number="PO-ORDER"):
        self.send(x12_order(po_number))
        self.post("/_mock/advance?all")
        return self.order(po_number)

    def documents_named(self, po_number="PO-ORDER"):
        """The consignment each of the 856 and the 810 points at."""
        despatch = self.document(ACME, "despatch").groups[0].messages[0]
        invoice = self.document(ACME, "invoice").groups[0].messages[0]
        references = {r.get(1): r.get(2) for r in invoice.find_all("REF")}
        return {
            "despatch_shipment": despatch.find("BSN").get(2),
            "despatch_bol": [r.get(2) for r in despatch.find_all("REF")
                             if r.get(1) == "BM"][0],
            "invoice_shipment": references.get("SI"),
            "invoice_bol": references.get("BM"),
        }


class InvoiceDueBeforeDespatch(OrderingCase):
    """The ordering that was broken: the invoice packs, then the despatch."""

    config_kwargs = {"despatch_delay_ms": HOUR, "invoice_delay_ms": 0}

    def test_the_order_has_one_shipment(self):
        order = self.fulfil()
        self.assertEqual(len(order["shipments"]), 1,
                         [s["shipment_id"] for s in order["shipments"]])

    def test_the_invoice_names_the_shipment_that_exists(self):
        order = self.fulfil()
        self.assertEqual(order["invoices"][0]["shipment_id"],
                         order["shipments"][0]["shipment_id"])

    def test_the_856_and_the_810_name_the_same_consignment(self):
        self.fulfil()
        named = self.documents_named()
        self.assertEqual(named["despatch_shipment"], named["invoice_shipment"])

    def test_and_the_same_bill_of_lading(self):
        self.fulfil()
        named = self.documents_named()
        self.assertEqual(named["despatch_bol"], named["invoice_bol"])

    def test_the_line_is_not_shipped_twice(self):
        order = self.fulfil()
        line = order["lines"][0]
        self.assertEqual(line["shipped"], line["confirmed"])

    def test_one_bill_of_lading_number_was_drawn(self):
        """A second shipment would burn a number from the range."""
        self.fulfil("PO-ONE")
        self.send(x12_order("PO-TWO"))
        self.post("/_mock/advance?all")
        first = self.order("PO-ONE")["shipments"][0]["bol"]
        second = self.order("PO-TWO")["shipments"][0]["bol"]
        self.assertEqual(int(second), int(first) + 1)


class DespatchDueBeforeInvoice(OrderingCase):
    """The default ordering, which always worked - and must keep working."""

    config_kwargs = {"despatch_delay_ms": 0, "invoice_delay_ms": HOUR}

    def test_one_shipment_and_matching_references(self):
        order = self.fulfil()
        self.assertEqual(len(order["shipments"]), 1)
        named = self.documents_named()
        self.assertEqual(named["despatch_shipment"], named["invoice_shipment"])
        self.assertEqual(named["despatch_bol"], named["invoice_bol"])


class BothDelayed(OrderingCase):
    config_kwargs = {"despatch_delay_ms": HOUR, "invoice_delay_ms": HOUR}

    def test_one_shipment_and_matching_references(self):
        order = self.fulfil()
        self.assertEqual(len(order["shipments"]), 1)
        named = self.documents_named()
        self.assertEqual(named["despatch_shipment"], named["invoice_shipment"])


class NoDelaysAtAll(OrderingCase):
    def test_one_shipment_and_matching_references(self):
        order = self.fulfil()
        self.assertEqual(len(order["shipments"]), 1)
        named = self.documents_named()
        self.assertEqual(named["despatch_shipment"], named["invoice_shipment"])


class PackingTheDifference(MockServerCase):
    """`create_shipment` packs confirmed-minus-shipped, not everything confirmed."""

    def test_calling_it_twice_does_not_pack_twice(self):
        from mockedi import documents
        self.send(x12_order("PO-TWICE"))
        conn = self.httpd.mock.conn
        with self.httpd.mock.lock:
            before = documents.latest_shipment(conn, "PO-TWICE")
            again = documents.create_shipment(conn, "PO-TWICE")
        self.assertEqual(again["shipment_id"], before["shipment_id"],
                         "a second call should name the consignment, not make one")
        rows = self.order("PO-TWICE")["shipments"]
        self.assertEqual(len(rows), 1)

    def test_an_order_with_nothing_confirmed_still_has_no_shipment(self):
        from mockedi import documents
        self.behaviour(ACME, "reject-all")
        self.send(x12_order("PO-NONE"))
        with self.httpd.mock.lock:
            shipment = documents.create_shipment(self.httpd.mock.conn, "PO-NONE")
        self.assertEqual(shipment, None)
        self.assertEqual(self.order("PO-NONE")["shipments"], [])
        self.assertEqual(self.order("PO-NONE")["status"], "rejected")

    def test_a_shipment_covers_only_what_it_packed(self):
        """The carton count follows the units in the consignment."""
        self.send(x12_order("PO-CARTONS",
                            lines=(("WIDGET-001", 48, "12.50"),)))
        shipment = self.order("PO-CARTONS")["shipments"][0]
        self.assertEqual(shipment["cartons"], 2)      # 48 units, 24 per carton
        self.assertEqual(shipment["weight"], "96")


if __name__ == "__main__":
    unittest.main(verbosity=2)
