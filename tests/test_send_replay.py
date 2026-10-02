"""A document sent on demand says what the original said (#201).

`/_mock/send` replays a despatch advice or an invoice. It wrote them from the
order's lines, which carry running totals over every consignment, where the
original was written from one consignment's. On an order that shipped twice
the replayed 810 billed the whole order's quantity against one shipment's
total, and the replayed 856 advised goods that were in the other box.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import (ACME, EURODIS, MockServerCase, edifact_change,
                     edifact_order, parse, x12_change, x12_order)

EDIFACT = {"Content-Type": "application/edifact"}
# Despatched at once and invoiced an hour later: the window in which a change
# can still add a second consignment.
WINDOW = {"invoice_delay_ms": 3600 * 1000}


def said(payload):
    """What a document says, apart from when this copy of it was written.

    The envelope and the set's own control number are new on a replay, which
    is the point of one; so are `BSN03/04` and a DESADV's `DTM+137`, the
    moment of writing. `SN105`, the quantity ordered, is left out too: a
    replay is written from the order as it stands, and a change since the
    original has moved it - `OrderedQuantityIsTheOrdersNow` holds that.
    Everything else is the document.
    """
    message = list(parse(payload).messages())[0][1]
    out = []
    for segment in message.body:
        elements = list(segment.elements)
        if segment.tag == "BSN":
            elements[2:4] = ["", ""]
        if segment.tag == "SN1" and len(elements) > 4:
            elements[4] = ""
        if (message.code == "DESADV" and segment.tag == "DTM"
                and segment.comp(1, 1) == "137"):
            continue
        out.append((segment.tag, elements))
    return out


class _TwoConsignments(MockServerCase):
    config_kwargs = WINDOW
    partner = ACME
    po = "PO-2C"

    def order_in_two_consignments(self):
        self.send(x12_order(self.po, lines=(("WIDGET-001", 100, "12.50"),)))
        self.send(x12_change(self.po, [("1", "QI", 150, "12.50")]))

    def setUp(self):
        super().setUp()
        self.order_in_two_consignments()
        self.post("/_mock/advance?all")
        self.shipments = [row["shipment_id"]
                          for row in self.order(self.po)["shipments"]]
        self.assertEqual(len(self.shipments), 2)
        self.originals = {kind: [row["payload"] for row in
                                 self.mailbox(self.partner, kind, leave=False)]
                          for kind in ("despatch", "invoice")}
        for kind in ("despatch", "invoice"):
            self.assertEqual(len(self.originals[kind]), 2, kind)

    def replay(self, kind, **more):
        body = dict({"partner": self.partner, "kind": kind, "order": self.po},
                    **more)
        status, _h, data = self.post("/_mock/send", body)
        self.assertEqual(status, 201, data)
        rows = self.mailbox(self.partner, kind, leave=False)
        self.assertEqual(len(rows), 1)
        return rows[0]["payload"]


class AReplaySaysWhatTheOriginalSaid(_TwoConsignments):

    def test_each_consignments_despatch_advice(self):
        for index, shipment in enumerate(self.shipments):
            with self.subTest(shipment=shipment):
                self.assertEqual(said(self.replay("despatch", shipment=shipment)),
                                 said(self.originals["despatch"][index]))

    def test_each_consignments_invoice(self):
        for index, shipment in enumerate(self.shipments):
            with self.subTest(shipment=shipment):
                self.assertEqual(said(self.replay("invoice", shipment=shipment)),
                                 said(self.originals["invoice"][index]))

    def test_with_no_shipment_named_it_is_the_latest(self):
        self.assertEqual(said(self.replay("despatch")),
                         said(self.originals["despatch"][1]))
        self.assertEqual(said(self.replay("invoice")),
                         said(self.originals["invoice"][1]))

    def test_the_replayed_invoice_adds_up(self):
        # The issue's own figures: 50 at 12.50 is 625.00, and the replay
        # billed 150 against that total.
        invoice = list(parse(self.replay("invoice")).messages())[0][1]
        self.assertEqual([(i.get(1), i.get(2)) for i in invoice.find_all("IT1")],
                         [("1", "50")])
        self.assertEqual(invoice.find("TDS").get(1), "62500")

    def test_a_replay_changes_nothing_about_the_order(self):
        before = self.order(self.po)
        self.replay("despatch", shipment=self.shipments[0])
        self.replay("invoice", shipment=self.shipments[0])
        after = self.order(self.po)
        self.assertEqual(after["lines"], before["lines"])
        self.assertEqual(len(after["shipments"]), 2)
        self.assertEqual(len(after["invoices"]), 2)

    def test_a_shipment_that_is_not_the_orders_is_refused(self):
        self.send(x12_order("PO-OTHER"))
        other = self.order("PO-OTHER")["shipments"][0]["shipment_id"]
        for shipment in ("SHP-NOPE", other):
            with self.subTest(shipment=shipment):
                status, _h, data = self.post("/_mock/send", {
                    "partner": ACME, "kind": "invoice", "order": self.po,
                    "shipment": shipment})
                self.assertEqual(status, 400, data)
                self.assertIn("has no shipment %r" % shipment, data["error"])
                for known in self.shipments:
                    self.assertIn(known, data["error"])


class OrderedQuantityIsTheOrdersNow(_TwoConsignments):
    """What shipped is the consignment's; what was ordered is the order's."""

    def test_the_first_consignment_replayed_after_the_change(self):
        original = list(parse(self.originals["despatch"][0]).messages())[0][1]
        replayed = list(parse(self.replay(
            "despatch", shipment=self.shipments[0])).messages())[0][1]
        # Shipped: 100 in this consignment, then and now.
        self.assertEqual(original.find("SN1").get(2), "100")
        self.assertEqual(replayed.find("SN1").get(2), "100")
        # Ordered: 100 when it was first advised, 150 since the change.
        self.assertEqual(original.find("SN1").get(5), "100")
        self.assertEqual(replayed.find("SN1").get(5), "150")


class AnEdifactReplaySaysWhatTheOriginalSaid(_TwoConsignments):
    partner = EURODIS
    po = "PO-E-2C"

    def order_in_two_consignments(self):
        self.send(edifact_order(self.po, lines=(("WIDGET-001", 100, "12.50"),)),
                  headers=EDIFACT)
        self.send(edifact_change(self.po, [("1", "3", 150, "12.50")]),
                  headers=EDIFACT)

    def test_each_consignments_documents(self):
        for kind in ("despatch", "invoice"):
            for index, shipment in enumerate(self.shipments):
                with self.subTest(kind=kind, shipment=shipment):
                    self.assertEqual(
                        said(self.replay(kind, shipment=shipment)),
                        said(self.originals[kind][index]))


class TheInvoiceNamesTheConsignmentItBills(MockServerCase):
    """The latest invoice and the latest shipment are not always a pair."""
    config_kwargs = {"despatch_delay_ms": 3600 * 1000,
                     "invoice_delay_ms": 2 * 3600 * 1000}

    def test_when_the_latest_shipment_is_not_billed_yet(self):
        self.send(x12_order("PO-HALF", lines=(("WIDGET-001", 100, "12.50"),)))
        self.post("/_mock/advance?seconds=3700")       # the first is packed
        self.send(x12_change("PO-HALF", [("1", "QI", 150, "12.50")]))
        self.post("/_mock/advance?seconds=3650")       # both packed, one billed
        order = self.order("PO-HALF")
        self.assertEqual((len(order["shipments"]), len(order["invoices"])), (2, 1))
        original = self.mailbox(ACME, "invoice", leave=False)[0]["payload"]
        status, _h, data = self.post("/_mock/send", {
            "partner": ACME, "kind": "invoice", "order": "PO-HALF"})
        self.assertEqual(status, 201, data)
        replayed = self.mailbox(ACME, "invoice", leave=False)[0]["payload"]
        self.assertEqual(said(replayed), said(original))


class AnOrderThatShippedOnce(MockServerCase):

    def test_replays_as_it_always_did(self):
        self.send(x12_order("PO-ONCE"))
        originals = {kind: self.mailbox(ACME, kind, leave=False)[0]["payload"]
                     for kind in ("despatch", "invoice")}
        for kind in ("despatch", "invoice"):
            with self.subTest(kind=kind):
                self.post("/_mock/send", {"partner": ACME, "kind": kind,
                                          "order": "PO-ONCE"})
                replayed = self.mailbox(ACME, kind, leave=False)[0]["payload"]
                self.assertEqual(said(replayed), said(originals[kind]))


del _TwoConsignments

if __name__ == "__main__":
    unittest.main()
