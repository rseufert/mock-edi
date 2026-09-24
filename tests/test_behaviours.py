"""Each partner behaviour, which is the reason this mock exists."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, EURODIS, GLOBEX, INITECH, MockServerCase, \
    edifact_order, x12_order


class Accept(MockServerCase):
    def test_everything_is_confirmed_as_ordered(self):
        self.send(x12_order("PO-ACCEPT"))
        lines = self.order("PO-ACCEPT")["lines"]
        self.assertEqual([l["status"] for l in lines], ["IA", "IA"])
        message = self.document(ACME, "response").groups[0].messages[0]
        self.assertEqual(message.find("BAK").get(2), "AD")


class ShortShip(MockServerCase):
    def setUp(self):
        MockServerCase.setUp(self)
        self.behaviour(ACME, "short-ship")
        self.send(x12_order("PO-SHORT"))

    def test_lines_come_back_quantity_changed(self):
        lines = self.order("PO-SHORT")["lines"]
        self.assertEqual([l["status"] for l in lines], ["IQ", "IQ"])
        self.assertEqual([l["confirmed"] for l in lines], ["80", "32"])

    def test_the_855_says_accepted_with_change(self):
        message = self.document(ACME, "response").groups[0].messages[0]
        self.assertEqual(message.find("BAK").get(2), "AC")
        self.assertEqual([a.get(2) for a in message.find_all("ACK")], ["80", "32"])

    def test_the_reason_is_carried_where_a_human_can_read_it(self):
        lines = self.order("PO-SHORT")["lines"]
        self.assertIn("Confirmed 80 of 100", lines[0]["reason"])

    def test_only_the_confirmed_quantity_ships_and_is_invoiced(self):
        message = self.document(ACME, "despatch").groups[0].messages[0]
        self.assertEqual([s.get(2) for s in message.find_all("SN1")], ["80", "32"])
        invoice = self.document(ACME, "invoice").groups[0].messages[0]
        # 80 x 12.50 + 32 x 4.15 = 1132.80
        self.assertEqual(invoice.find("TDS").get(1), "113280")

    def test_in_edifact_the_shortfall_is_a_backorder_quantity(self):
        self.behaviour(EURODIS, "short-ship")
        self.send(edifact_order("PO-SHORT-E"),
                  headers={"Content-Type": "application/edifact"})
        message = self.document(EURODIS, "response").groups[0].messages[0]
        backorder = [q for q in message.find_all("QTY") if q.comp(1, 1) == "83"]
        self.assertEqual([q.comp(1, 2) for q in backorder], ["20", "8"])


class RejectLine(MockServerCase):
    def setUp(self):
        MockServerCase.setUp(self)
        self.behaviour(ACME, "reject-line")
        self.send(x12_order("PO-REJECT-LINE"))

    def test_the_last_line_is_refused(self):
        lines = self.order("PO-REJECT-LINE")["lines"]
        self.assertEqual([l["status"] for l in lines], ["IA", "IR"])
        self.assertEqual(lines[1]["confirmed"], "0")

    def test_a_rejected_line_commits_to_no_date(self):
        message = self.document(ACME, "response").groups[0].messages[0]
        rejected = [a for a in message.find_all("ACK") if a.get(1) == "IR"][0]
        self.assertEqual(rejected.get(2), "0")
        self.assertEqual(rejected.get(5), "")

    def test_it_is_left_out_of_the_shipment_and_the_invoice(self):
        despatch = self.document(ACME, "despatch").groups[0].messages[0]
        self.assertEqual(len(despatch.find_all("SN1")), 1)
        invoice = self.document(ACME, "invoice").groups[0].messages[0]
        self.assertEqual(len(invoice.find_all("IT1")), 1)
        self.assertEqual(invoice.find("TDS").get(1), "125000")   # 100 x 12.50


class RejectAll(MockServerCase):
    def setUp(self):
        MockServerCase.setUp(self)
        self.behaviour(ACME, "reject-all")
        self.summary = self.send(x12_order("PO-REJECT-ALL"))

    def test_the_syntax_is_still_acknowledged(self):
        message = self.document(ACME, "acknowledgment").groups[0].messages[0]
        self.assertEqual(message.find("AK5").get(1), "A")

    def test_the_855_refuses_the_order(self):
        message = self.document(ACME, "response").groups[0].messages[0]
        self.assertEqual(message.find("BAK").get(2), "RJ")

    def test_nothing_ships_and_nothing_is_invoiced(self):
        self.assertEqual([q["code"] for q in self.summary["queued"]], ["997", "855"])
        order = self.order("PO-REJECT-ALL")
        self.assertEqual(order["shipments"], [])
        self.assertEqual(order["invoices"], [])
        self.assertEqual(order["status"], "rejected")


class NoAcknowledgment(MockServerCase):
    def setUp(self):
        MockServerCase.setUp(self)
        self.behaviour(ACME, "no-ack")
        self.summary = self.send(x12_order("PO-SILENT"))

    def test_nothing_at_all_comes_back(self):
        self.assertEqual(self.summary["queued"], [])
        self.assertEqual(self.mailbox(ACME), [])

    def test_the_order_is_still_recorded_so_the_test_can_see_it_arrived(self):
        self.assertEqual(self.order("PO-SILENT")["po_number"], "PO-SILENT")


class DuplicateInvoice(MockServerCase):
    def setUp(self):
        MockServerCase.setUp(self)
        self.behaviour(ACME, "duplicate-invoice")
        self.send(x12_order("PO-DUP"))
        self.post("/_mock/advance?all")

    def test_the_invoice_arrives_twice(self):
        invoices = self.mailbox(ACME, "invoice")
        self.assertEqual(len(invoices), 2)

    def test_both_carry_the_same_invoice_number(self):
        invoices = self.mailbox(ACME, "invoice")
        numbers = []
        for row in invoices:
            from support import parse
            message = parse(row["payload"]).groups[0].messages[0]
            numbers.append(message.find("BIG").get(2))
        self.assertEqual(numbers[0], numbers[1])

    def test_the_second_one_says_what_it_is(self):
        rows = [r for r in self.mailbox(ACME, "invoice") if r["note"]]
        self.assertEqual(len(rows), 1)
        self.assertIn("duplicate", rows[0]["note"])


class Strict(MockServerCase):
    def test_a_finding_that_others_accept_is_refused(self):
        from mockedi.envelope import seg
        self.behaviour(ACME, "strict")
        # An unknown code in BEG01: an error, but not a fatal one.
        summary = self.send(x12_order("PO-STRICT", purpose="ZZ"))
        self.assertFalse(summary["accepted"])
        self.assertEqual(summary["orders"], [])

    def test_the_same_document_is_accepted_by_a_lenient_partner(self):
        summary = self.send(x12_order("PO-LENIENT", purpose="ZZ"))
        self.assertTrue(summary["accepted"])


class UnknownItems(MockServerCase):
    """A catalogue miss outranks whatever the behaviour says."""

    def test_an_unknown_sku_is_rejected_even_by_an_accepting_partner(self):
        self.send(x12_order("PO-UNKNOWN",
                            lines=(("NO-SUCH-SKU", 5, "1.00"),
                                   ("WIDGET-001", 10, "12.50"))))
        lines = self.order("PO-UNKNOWN")["lines"]
        self.assertEqual([l["status"] for l in lines], ["IR", "IA"])
        self.assertIn("not in the catalogue", lines[0]["reason"])

    def test_a_price_the_seller_disagrees_with_is_flagged_and_overridden(self):
        self.send(x12_order("PO-PRICE", lines=(("WIDGET-001", 10, "99.99"),)))
        line = self.order("PO-PRICE")["lines"][0]
        self.assertEqual(line["status"], "IP")
        self.assertEqual(line["price"], "12.50")
        self.assertEqual(line["ordered_price"], "99.99")
        invoice = self.document(ACME, "invoice").groups[0].messages[0]
        self.assertEqual(invoice.find("TDS").get(1), "12500")   # 10 x 12.50

    def test_an_item_ordered_by_upc_is_found_and_answered_by_sku(self):
        self.send(x12_order("PO-UPC", qualifier="UP",
                            lines=(("076123400003", 10, "12.50"),)))
        line = self.order("PO-UPC")["lines"][0]
        self.assertEqual(line["sku"], "WIDGET-001")
        self.assertEqual(line["upc"], "076123400003")


class StockLimits(MockServerCase):
    def test_a_line_larger_than_stock_is_confirmed_short_by_any_partner(self):
        # GEAR-200 is seeded with 60 in stock.
        self.send(x12_order("PO-STOCK", lines=(("GEAR-200", 500, "14.25"),)))
        line = self.order("PO-STOCK")["lines"][0]
        self.assertEqual(line["status"], "IQ")
        self.assertEqual(line["confirmed"], "60")


if __name__ == "__main__":
    unittest.main(verbosity=2)
