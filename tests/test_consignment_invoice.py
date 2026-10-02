"""A consignment a change adds is advised before it is billed (#222).

An 860 that raises a shipped line confirms goods nothing is left to pack, so
a second despatch is promised. The first consignment's invoice was still
waiting, and when it came due before that despatch it packed the new goods
itself and billed them: the 810 went out before the 856 that advised them,
which is what the mock as buyer reports a supplier for. The second
consignment now has an invoice promise of its own, and the first invoice is
neither late nor billing what has not shipped.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import (ACME, EURODIS, MockServerCase, edifact_change,
                     edifact_order, parse, x12_change, x12_order)

HOUR = 3600
EDIFACT = {"Content-Type": "application/edifact"}


class _Consignments(MockServerCase):
    partner = ACME
    despatch, invoice = "856", "810"

    def order_then_raise_after_despatch(self, po_number):
        """100 and 40 ordered and despatched, then line 1 raised to 150."""
        self.send(x12_order(po_number))
        self.post("/_mock/advance?seconds=%d" % (HOUR + 100))
        self.send(x12_change(po_number, [("1", "QI", 150, "12.50")]))

    def sent(self, po_number):
        """The despatch advices and invoices for the order, in the order sent."""
        _s, _h, rows = self.get("/_mock/outbox?limit=1000")
        return [row["code"] for row in sorted(rows, key=lambda row: row["id"])
                if row["reference"] == po_number
                and row["code"] in (self.despatch, self.invoice)]

    def waiting(self):
        _s, _h, rows = self.get("/_mock/scheduled")
        return [row["kind"] for row in rows]


class RaisedAfterDespatchWithTheInvoiceDueFirst(_Consignments):
    """Despatch after an hour, invoice after two; the change comes in between."""

    config_kwargs = {"despatch_delay_ms": HOUR * 1000,
                     "invoice_delay_ms": 2 * HOUR * 1000}

    def test_the_second_consignment_is_advised_before_it_is_billed(self):
        self.order_then_raise_after_despatch("PO-2C")
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent("PO-2C"), [self.despatch, self.invoice,
                                              self.despatch, self.invoice])

    def test_it_is_promised_an_invoice_of_its_own(self):
        self.order_then_raise_after_despatch("PO-2C")
        self.assertEqual(self.waiting(), ["invoice", "despatch", "invoice"])

    def test_the_first_invoice_goes_out_when_it_was_due_and_bills_what_shipped(self):
        self.order_then_raise_after_despatch("PO-2C")
        # Two hours after the order: the first invoice is due, and the second
        # despatch, an hour after the change, is not.
        self.post("/_mock/advance?seconds=%d" % (HOUR - 100))
        self.assertEqual(self.sent("PO-2C"), [self.despatch, self.invoice])
        self.assertEqual(self.waiting(), ["despatch", "invoice"])
        order = self.order("PO-2C")
        self.assertEqual(len(order["shipments"]), 1)
        self.assertEqual([(row["shipped"], row["invoiced"])
                          for row in order["lines"]],
                         [("100", "100"), ("40", "40")])

    def test_each_invoice_names_the_consignment_it_bills(self):
        self.order_then_raise_after_despatch("PO-2C")
        self.post("/_mock/advance?all")
        order = self.order("PO-2C")
        invoices = [parse(row["payload"]).groups[0].messages[0]
                    for row in self.mailbox(ACME, "invoice")]
        named = [[r.get(2) for r in m.find_all("REF") if r.get(1) == "SI"][0]
                 for m in invoices]
        self.assertEqual(named, [s["shipment_id"] for s in order["shipments"]])
        self.assertEqual([[(i.get(1), i.get(2)) for i in m.find_all("IT1")]
                          for m in invoices],
                         [[("1", "100"), ("2", "40")], [("1", "50")]])
        for line in order["lines"]:
            self.assertEqual((line["shipped"], line["invoiced"]),
                             (line["confirmed"], line["confirmed"]))

    def test_an_out_of_order_partner_still_bills_first(self):
        # Its invoice and despatch come due together, invoice first, so the
        # order is invoiced before a change could add anything to it.
        self.patch("/_mock/partners/ACME", {"behaviour": "out-of-order"})
        self.send(x12_order("PO-OOO"))
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent("PO-OOO"), [self.invoice, self.despatch])


class AnOrdersRaisedAfterDespatchWithTheInvoiceDueFirst(_Consignments):
    partner = EURODIS
    despatch, invoice = "DESADV", "INVOIC"
    config_kwargs = {"despatch_delay_ms": HOUR * 1000,
                     "invoice_delay_ms": 2 * HOUR * 1000}

    def test_the_second_consignment_is_advised_before_it_is_billed(self):
        self.send(edifact_order("PO-E-2C"), headers=EDIFACT)
        self.post("/_mock/advance?seconds=%d" % (HOUR + 100))
        self.send(edifact_change("PO-E-2C", [("1", "3", 150, "12.50")]),
                  headers=EDIFACT)
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent("PO-E-2C"), ["DESADV", "INVOIC",
                                                "DESADV", "INVOIC"])


class RaisedAfterDespatchWithTheInvoiceStillFarOff(_Consignments):
    """The waiting invoice is due after the new despatch, and bills both."""

    config_kwargs = {"despatch_delay_ms": HOUR * 1000,
                     "invoice_delay_ms": 4 * HOUR * 1000}

    def test_one_invoice_promise_bills_both_once_both_are_advised(self):
        self.order_then_raise_after_despatch("PO-LATE")
        self.assertEqual(self.waiting(), ["despatch", "invoice"])
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent("PO-LATE"), ["856", "856", "810", "810"])


class TheInvoiceConfiguredAheadOfTheDespatch(_Consignments):
    """A seller that bills as it ships: that is what the delays ask for."""

    config_kwargs = {"despatch_delay_ms": 2 * HOUR * 1000,
                     "invoice_delay_ms": HOUR * 1000}

    def test_the_invoice_still_packs_and_goes_first(self):
        self.send(x12_order("PO-BILL-FIRST"))
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent("PO-BILL-FIRST"), ["810", "856"])
        self.assertEqual(len(self.order("PO-BILL-FIRST")["shipments"]), 1)


class WithdrawingAnOrdersFulfilment(unittest.TestCase):
    """Only the packing and the billing are withdrawn, whoever asks."""

    def test_a_promise_of_another_kind_is_left_waiting(self):
        from mockedi import db, pipeline, schema
        from mockedi.server import Config
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        db.seed(conn, 1, "MOCKEDI")
        line = pipeline.Pipeline(conn, Config(db_path=":memory:"))
        for kind in (schema.DESPATCH, schema.INVOICE, pipeline.CHANGE_LINE):
            conn.execute(
                "INSERT INTO scheduled (partner, po_number, kind, due_at, at)"
                " VALUES ('ACME', 'PO-W', ?, ?, ?)", (kind, db.now(), db.now()))
        line._withdraw_fulfilment({"id": "ACME"}, "PO-W", "order restated")
        self.assertEqual(
            {row["kind"]: row["note"] for row in db.rows(
                conn, "SELECT kind, note FROM scheduled WHERE done_at != ''")},
            {schema.DESPATCH: "order restated", schema.INVOICE: "order restated"})
        self.assertEqual(
            [row["kind"] for row in db.rows(
                conn, "SELECT kind FROM scheduled WHERE done_at = ''")],
            [pipeline.CHANGE_LINE])


del _Consignments

if __name__ == "__main__":
    unittest.main()
