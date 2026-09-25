"""Changing an order that has already been sent."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from decimal import Decimal

from mockedi import validate

from support import (ACME, EURODIS, MockServerCase, edifact_change,
                     edifact_order, parse, x12_change, x12_order)

# A change is only meaningful before the goods leave. With every delay at
# zero the order is invoiced before the POST returns, so these tests give
# themselves a window - which is exactly what the README tells a user to do.
WINDOW = {"despatch_delay_ms": 3600000, "invoice_delay_ms": 3600000}


class ChangingAQuantity(MockServerCase):
    config_kwargs = WINDOW

    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-CHANGE"))
        self.mailbox(ACME, leave=False)

    def test_the_order_now_says_what_the_change_asked_for(self):
        self.send(x12_change("PO-CHANGE", [("1", "CA", 60, "12.50")]))
        line = self.order("PO-CHANGE")["lines"][0]
        self.assertEqual(line["quantity"], "60")
        self.assertEqual(line["confirmed"], "60")
        self.assertEqual(line["status"], "IA")

    def test_an_865_comes_back(self):
        summary = self.send(x12_change("PO-CHANGE", [("1", "CA", 60, "12.50")]))
        self.assertEqual([q["code"] for q in summary["queued"]], ["997", "865"])
        message = self.document(ACME, "change-response").groups[0].messages[0]
        self.assertEqual(message.code, "865")
        self.assertEqual(message.find("BCA").get(3), "PO-CHANGE")
        self.assertEqual(message.find("BCA").get(5), "1")   # change sequence

    def test_the_865_puts_the_po_date_in_bca10(self):
        # By position, from 004010: 326, 367 and 127 at 07-09, the purchase
        # order date at 10.
        self.send(x12_change("PO-CHANGE", [("1", "CA", 60, "12.50")]))
        bca = self.document(ACME, "change-response").groups[0].messages[0].find("BCA")
        self.assertEqual([bca.get(7), bca.get(8), bca.get(9)], ["", "", ""])
        self.assertEqual(bca.get(10), "20260924")

    def test_the_865_answers_line_by_line_in_the_855s_vocabulary(self):
        self.send(x12_change("PO-CHANGE", [("1", "CA", 60, "12.50")]))
        message = self.document(ACME, "change-response").groups[0].messages[0]
        poc = message.find("POC")
        self.assertEqual((poc.get(1), poc.get(2), poc.get(3)), ("1", "CA", "60"))

    def test_the_verb_the_buyer_used_is_echoed_back(self):
        self.send(x12_change("PO-CHANGE", [("1", "QD", 60, "12.50")]))
        message = self.document(ACME, "change-response").groups[0].messages[0]
        self.assertEqual(message.find("POC").get(2), "QD")
        self.assertEqual(message.find("ACK").get(1), "IA")
        self.assertEqual(message.find("ACK").get(2), "60")

    def test_the_total_is_recalculated(self):
        before = self.order("PO-CHANGE")["total"]
        self.send(x12_change("PO-CHANGE", [("1", "CA", 60, "12.50")]))
        after = self.order("PO-CHANGE")["total"]
        self.assertNotEqual(before, after)
        # 60 x 12.50 + 40 x 4.15 = 916.00
        self.assertEqual(after, "916.00")

    def test_raising_beyond_stock_is_confirmed_short(self):
        self.send(x12_change("PO-CHANGE", [("1", "CA", 99999, "12.50")]))
        line = self.order("PO-CHANGE")["lines"][0]
        self.assertEqual(line["status"], "IQ")
        self.assertEqual(line["confirmed"], "4200")     # what is in stock
        self.assertIn("not available", line["reason"])

    def test_the_despatch_when_it_comes_reflects_the_change(self):
        """The reason fulfilment is scheduled rather than done eagerly."""
        self.send(x12_change("PO-CHANGE", [("1", "CA", 60, "12.50")]))
        self.mailbox(ACME, leave=False)
        self.post("/_mock/advance?all")
        despatch = self.document(ACME, "despatch").groups[0].messages[0]
        self.assertEqual([s.get(2) for s in despatch.find_all("SN1")], ["60", "40"])
        invoice = self.document(ACME, "invoice").groups[0].messages[0]
        self.assertEqual(invoice.find("TDS").get(1), "91600")


class AddingAndDeletingLines(MockServerCase):
    config_kwargs = WINDOW

    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-LINES"))
        self.mailbox(ACME, leave=False)

    def test_a_line_can_be_deleted(self):
        self.send(x12_change("PO-LINES", [("2", "DI", 0, "0")]))
        lines = self.order("PO-LINES")["lines"]
        self.assertEqual(lines[1]["status"], "IR")
        self.assertEqual(lines[1]["confirmed"], "0")
        self.assertIn("deleted", lines[1]["reason"].lower())

    def test_a_deleted_line_is_left_out_of_the_shipment(self):
        self.send(x12_change("PO-LINES", [("2", "DI", 0, "0")]))
        self.mailbox(ACME, leave=False)
        self.post("/_mock/advance?all")
        despatch = self.document(ACME, "despatch").groups[0].messages[0]
        self.assertEqual(len(despatch.find_all("SN1")), 1)

    def test_a_line_can_be_added(self):
        self.send(x12_change("PO-LINES", [("3", "AI", 25, "8.90")],
                             skus={"3": "GEAR-100"}))
        lines = self.order("PO-LINES")["lines"]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[2]["sku"], "GEAR-100")
        self.assertEqual(lines[2]["confirmed"], "25")

    def test_deleting_a_line_that_is_not_there_is_refused_not_ignored(self):
        self.send(x12_change("PO-LINES", [("9", "DI", 0, "0")]))
        message = self.document(ACME, "change-response").groups[0].messages[0]
        self.assertEqual(message.find("ACK").get(1), "IR")


class CancellingAnOrder(MockServerCase):
    config_kwargs = WINDOW

    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-CANCEL"))
        self.mailbox(ACME, leave=False)

    def test_a_cancellation_before_despatch_is_taken(self):
        self.send(x12_change("PO-CANCEL", [("1", "DI", 0, "0")], purpose="01"))
        order = self.order("PO-CANCEL")
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order["total"], "0.00")
        self.assertTrue(all(line["confirmed"] == "0" for line in order["lines"]))

    def test_nothing_is_packed_or_billed_afterwards(self):
        self.send(x12_change("PO-CANCEL", [("1", "DI", 0, "0")], purpose="01"))
        self.mailbox(ACME, leave=False)
        self.post("/_mock/advance?all")
        self.assertEqual(self.mailbox(ACME, "despatch"), [])
        self.assertEqual(self.mailbox(ACME, "invoice"), [])
        self.assertEqual(self.order("PO-CANCEL")["status"], "cancelled")

    def test_the_scheduled_work_is_marked_off(self):
        self.send(x12_change("PO-CANCEL", [("1", "DI", 0, "0")], purpose="01"))
        _status, _headers, rows = self.get("/_mock/scheduled")
        self.assertEqual(rows, [])


class ChangesThatCannotBeMade(MockServerCase):
    """A change cannot unmake what has already happened."""

    def test_a_change_after_the_invoice_is_refused(self):
        # No window: the default delays invoice the order immediately.
        self.send(x12_order("PO-GONE"))
        summary = self.send(x12_change("PO-GONE", [("1", "CA", 60, "12.50")]))
        self.assertEqual([q["code"] for q in summary["queued"]], ["997"])
        self.assertEqual(summary["changed"], [])
        self.assertEqual(self.order("PO-GONE")["lines"][0]["quantity"], "100")

    def test_and_says_why(self):
        self.send(x12_order("PO-GONE"))
        summary = self.send(x12_change("PO-GONE", [("1", "CA", 60, "12.50")]))
        self.assertEqual(len(summary["refusals"]), 1)
        self.assertIn("invoiced", summary["refusals"][0]["reason"])

    def test_a_change_to_an_order_we_never_saw_is_refused_not_created(self):
        summary = self.send(x12_change("PO-UNKNOWN", [("1", "CA", 5, "1.00")]))
        self.assertIn("no such purchase order", summary["refusals"][0]["reason"])
        status, _headers, _data = self.get("/_mock/orders/PO-UNKNOWN")
        self.assertEqual(status, 404)

    def test_the_syntax_is_still_acknowledged_even_when_the_change_is_refused(self):
        self.send(x12_order("PO-GONE"))
        self.mailbox(ACME, leave=False)
        self.send(x12_change("PO-GONE", [("1", "CA", 60, "12.50")]))
        message = self.document(ACME, "acknowledgment").groups[0].messages[0]
        self.assertEqual(message.find("AK5").get(1), "A")


class ChangesAgainstShippedGoods(MockServerCase):
    config_kwargs = {"invoice_delay_ms": 3600000}

    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-SHIPPED"))       # despatched, not yet invoiced
        self.mailbox(ACME, leave=False)

    def test_a_quantity_cannot_be_lowered_below_what_shipped(self):
        self.send(x12_change("PO-SHIPPED", [("1", "CA", 10, "12.50")]))
        # The order keeps what it had: the request was refused, not applied.
        self.assertEqual(self.order("PO-SHIPPED")["lines"][0]["quantity"], "100")

    def test_and_the_865_says_the_line_was_refused_and_why(self):
        self.send(x12_change("PO-SHIPPED", [("1", "CA", 10, "12.50")]))
        message = self.document(ACME, "change-response").groups[0].messages[0]
        self.assertEqual(message.find("ACK").get(1), "IR")
        reason = [r for r in message.find_all("REF") if r.get(1) == "ZZ"][0]
        self.assertIn("already shipped", reason.get(3))

    def test_a_shipped_line_cannot_be_deleted(self):
        self.send(x12_change("PO-SHIPPED", [("1", "DI", 0, "0")]))
        self.assertEqual(self.order("PO-SHIPPED")["lines"][0]["confirmed"], "100")
        message = self.document(ACME, "change-response").groups[0].messages[0]
        self.assertEqual(message.find("ACK").get(1), "IR")

    def test_the_order_cannot_be_cancelled_once_it_has_shipped(self):
        summary = self.send(
            x12_change("PO-SHIPPED", [("1", "DI", 0, "0")], purpose="01"))
        self.assertIn("shipped", summary["refusals"][0]["reason"])
        self.assertNotEqual(self.order("PO-SHIPPED")["status"], "cancelled")

    def test_raising_a_shipped_line_is_allowed(self):
        self.send(x12_change("PO-SHIPPED", [("1", "CA", 150, "12.50")]))
        line = self.order("PO-SHIPPED")["lines"][0]
        self.assertEqual(line["quantity"], "150")
        self.assertEqual(line["confirmed"], "150")


class WhatAChangeConfirmsIsDelivered(MockServerCase):
    """Every quantity an 865 confirms is shipped and billed - or refused.

    The order has already despatched (the despatch delay is zero) and is
    waiting to be invoiced, which is the window in which a change can still
    raise a quantity or add a line. What it adds goes out as a second
    consignment, with an 856 and an 810 of its own.
    """

    config_kwargs = {"invoice_delay_ms": 3600000}

    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-MORE"))      # 100 + 40, despatched at once
        self.mailbox(ACME, leave=False)

    def settled(self):
        self.post("/_mock/advance?all")
        order = self.order("PO-MORE")
        for line in order["lines"]:
            self.assertEqual((line["shipped"], line["invoiced"]),
                             (line["confirmed"], line["confirmed"]),
                             "line %s" % line["line"])
        return order

    def documents(self, kind):
        return [parse(row["payload"]).groups[0].messages[0]
                for row in self.mailbox(ACME, kind)]

    def test_a_raised_quantity_ships_as_a_second_consignment(self):
        self.send(x12_change("PO-MORE", [("1", "QI", 150, "12.50")]))
        order = self.settled()
        self.assertEqual(order["lines"][0]["confirmed"], "150")
        first, second = [s["shipment_id"] for s in order["shipments"]]

        # The second 856 carries the difference, not the running total.
        despatch = self.documents("despatch")
        self.assertEqual(len(despatch), 1)
        self.assertEqual(despatch[0].find("BSN").get(2), second)
        self.assertEqual([(s.get(1), s.get(2)) for s in despatch[0].find_all("SN1")],
                         [("1", "50")])

    def test_each_consignment_is_billed_once_and_named_by_its_own_810(self):
        self.send(x12_change("PO-MORE", [("1", "QI", 150, "12.50")]))
        order = self.settled()
        invoices = self.documents("invoice")
        self.assertEqual(len(invoices), 2)
        named = [[r.get(2) for r in m.find_all("REF") if r.get(1) == "SI"][0]
                 for m in invoices]
        self.assertEqual(named, [s["shipment_id"] for s in order["shipments"]])
        billed = [[(i.get(1), i.get(2)) for i in m.find_all("IT1")] for m in invoices]
        self.assertEqual(billed, [[("1", "100"), ("2", "40")], [("1", "50")]])
        # 1416.00 for the first consignment, 625.00 for the second: the
        # order's total is what was billed, and it adds up.
        for message in invoices + self.documents("despatch"):
            report = validate.validate_message(message, "X12")
            self.assertTrue(report.clean, report.summary())
        tds = sum(Decimal(m.find("TDS").get(1)) / 100 for m in invoices)
        self.assertEqual(tds, Decimal("2041.00"))
        self.assertEqual(Decimal(order["total"]), tds)

    def test_a_line_added_after_despatch_is_shipped_and_billed(self):
        self.send(x12_change("PO-MORE", [("3", "AI", 20, "8.90")],
                             skus={"3": "GEAR-100"}))
        order = self.settled()
        self.assertEqual(order["lines"][2]["confirmed"], "20")
        self.assertEqual(len(order["shipments"]), 2)

    def test_a_change_that_confirms_nothing_new_schedules_nothing(self):
        self.send(x12_change("PO-MORE", [("1", "PC", 100, "12.00")]))
        _status, _headers, rows = self.get("/_mock/scheduled")
        despatches = [r for r in rows if r["po_number"] == "PO-MORE"
                      and r["kind"] == "despatch" and not r["done_at"]]
        self.assertEqual(despatches, [])


class Reviving(MockServerCase):
    """A change to a cancelled order that confirms something again."""

    config_kwargs = WINDOW

    def test_a_revived_order_is_fulfilled(self):
        self.send(x12_order("PO-REVIVE"))
        self.send(x12_change("PO-REVIVE", [("1", "DI", 0, "0")], purpose="01"))
        self.send(x12_change("PO-REVIVE", [("1", "CA", 30, "12.50")],
                             control="000000079"))
        self.post("/_mock/advance?all")
        order = self.order("PO-REVIVE")
        line = order["lines"][0]
        self.assertEqual((line["confirmed"], line["shipped"], line["invoiced"]),
                         ("30", "30", "30"))
        self.assertEqual(order["status"], "invoiced")


class TheEdifactSideAfterDespatch(MockServerCase):
    config_kwargs = {"invoice_delay_ms": 3600000}

    def test_the_second_desadv_carries_the_difference(self):
        self.send(edifact_order("PO-E-MORE"),
                  headers={"Content-Type": "application/edifact"})
        self.mailbox(EURODIS, leave=False)
        self.send(edifact_change("PO-E-MORE", [("1", "3", 150, "12.50")]),
                  headers={"Content-Type": "application/edifact"})
        self.post("/_mock/advance?all")
        desadv = self.document(EURODIS, "despatch").groups[0].messages[0]
        self.assertEqual([q.comp(1, 2) for q in desadv.find_all("QTY")], ["50"])
        self.assertEqual(len(self.mailbox(EURODIS, "invoice")), 2)


class AnOrderRestatedAsAChange(MockServerCase):
    """BEG01 = 04 against an order the mock already holds."""

    config_kwargs = WINDOW

    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-RESTATE"))
        self.mailbox(ACME, leave=False)

    def test_it_changes_rather_than_replaces(self):
        summary = self.send(x12_order(
            "PO-RESTATE", purpose="04",
            lines=(("WIDGET-001", 70, "12.50"), ("BRKT-050", 40, "4.15"))))
        self.assertEqual([q["code"] for q in summary["queued"]], ["997", "865"])
        self.assertEqual(self.order("PO-RESTATE")["lines"][0]["quantity"], "70")

    def test_a_first_order_with_a_change_purpose_is_still_an_order(self):
        summary = self.send(x12_order("PO-BRAND-NEW", purpose="04"))
        self.assertEqual(summary["orders"], ["PO-BRAND-NEW"])
        self.assertEqual(self.order("PO-BRAND-NEW")["lines"][0]["quantity"], "100")


class TheEdifactSide(MockServerCase):
    config_kwargs = WINDOW

    def setUp(self):
        super().setUp()
        self.send(edifact_order("PO-E-CHANGE"),
                  headers={"Content-Type": "application/edifact"})
        self.mailbox(EURODIS, leave=False)

    def change(self, lines, purpose="4"):
        return self.send(edifact_change("PO-E-CHANGE", lines, purpose=purpose),
                         headers={"Content-Type": "application/edifact"})

    def test_an_ordchg_changes_the_order(self):
        self.change([("1", "3", 60, "12.50")])
        self.assertEqual(self.order("PO-E-CHANGE")["lines"][0]["quantity"], "60")

    def test_it_is_answered_by_an_ordrsp_not_a_message_of_its_own(self):
        summary = self.change([("1", "3", 60, "12.50")])
        self.assertEqual([q["code"] for q in summary["queued"]],
                         ["CONTRL", "ORDRSP"])

    def test_the_response_carries_the_action_taken_on_each_line(self):
        self.change([("1", "3", 60, "12.50")])
        message = self.document(EURODIS, "change-response").groups[0].messages[0]
        self.assertEqual(message.find("LIN").get(2), "3")       # changed
        confirmed = [q for q in message.find_all("QTY") if q.comp(1, 1) == "113"]
        self.assertEqual(confirmed[0].comp(1, 2), "60")

    def test_a_line_can_be_deleted(self):
        self.change([("2", "2", 0, "0")])
        self.assertEqual(self.order("PO-E-CHANGE")["lines"][1]["confirmed"], "0")

    def test_a_cancellation_uses_the_message_function_code(self):
        self.change([("1", "2", 0, "0")], purpose="1")
        self.assertEqual(self.order("PO-E-CHANGE")["status"], "cancelled")


class TheDictionaryKnowsThem(MockServerCase):
    def test_the_new_sets_are_published(self):
        _s, _h, data = self.get("/_mock/dictionary")
        codes = {(t["dialect"], t["code"]): t for t in data["transactionSets"]}
        self.assertEqual(codes[("X12", "860")]["kind"], "change")
        self.assertEqual(codes[("X12", "865")]["kind"], "change-response")
        self.assertEqual(codes[("EDIFACT", "ORDCHG")]["kind"], "change")

    def test_the_860_definition_carries_poc_and_its_change_codes(self):
        _s, _h, data = self.get("/_mock/dictionary/X12/860")
        poc = [s for s in data["segments"] if s["tag"] == "POC"][0]
        self.assertEqual(poc["elements"][1]["ref"], "670")
        self.assertIn("QD", poc["elements"][1]["codes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
