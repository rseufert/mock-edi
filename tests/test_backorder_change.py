"""A balance waiting for stock can still be lowered or cancelled (#337).

With no delays an order is invoiced the moment its first consignment is
billed, and an invoiced order cannot be changed. That rule met #335's
backorder badly: a buyer told "65 to follow" who no longer wanted them had
its change refused, and the 65 shipped and were billed on the day.

What is waiting has not shipped and has not been billed, so a change that is
only about that is taken: the balance comes down, or goes, and the 865 or
ORDRSP says what came off. Anything else in a change to an invoiced order
refuses the whole of it, as before.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import documents

from support import (ACME, EURODIS, edifact_change, edifact_order, parse,
                     x12_change, x12_order)
from test_backorder import (EDIFACT_TYPE, PRICE, SCARCE, SHORT_ORDER,
                            BackorderCase)

SKUS = {"1": SCARCE, "2": "WIDGET-001"}


class ChangeCase(BackorderCase):
    """100 of an item with 35 in stock: 35 shipped and billed, 65 waiting."""

    def setUp(self):
        super().setUp()
        self.send(x12_order(self.po, lines=SHORT_ORDER))
        self.assertEqual(self.order(self.po)["status"], "invoiced")
        self.mailbox(ACME, leave=False)

    def change(self, *lines, **kw):
        return self.send(x12_change(self.po, list(lines), skus=SKUS, **kw))

    def answer(self):
        message = self.document(ACME, "change-response").groups[0].messages[0]
        return [ref.get(3) for ref in message.find_all("REF")
                if ref.get(1) == "ZZ"]


class CancellingTheBalance(ChangeCase):

    def setUp(self):
        super().setUp()
        self.receipt = self.change(("1", "QD", 35, PRICE))

    def test_the_change_is_taken_not_refused(self):
        self.assertEqual(self.receipt.get("refusals") or [], [])

    def test_the_line_is_whole_at_what_shipped(self):
        line = self.line()
        self.assertEqual(
            [line[name] for name in ("quantity", "confirmed", "shipped",
                                     "backordered", "status", "reason")],
            ["35", "35", "35", "0", "IA", ""])

    def test_the_865_says_what_came_off(self):
        self.assertEqual(self.answer(),
                         ["65 no longer to follow, at the buyer's request"])

    def test_the_promise_is_closed_unkept_and_says_why(self):
        (promise,) = self.scheduled("backorder")
        self.assertNotEqual(promise["done_at"], "")
        self.assertEqual(promise["note"],
                         "nothing is waiting for stock that day")

    def test_nothing_more_ships_or_is_billed(self):
        self.advance()
        self.assertEqual(len(self.sent("856")), 1)
        self.assertEqual(len(self.sent("810")), 1)

    def test_the_order_is_still_invoiced_for_what_it_was(self):
        order = self.order(self.po)
        self.assertEqual((order["status"], order["total"]),
                         ("invoiced", "4420.00"))


class LoweringTheBalance(ChangeCase):

    def setUp(self):
        super().setUp()
        self.day = self.line()["scheduled_on"]
        self.receipt = self.change(("1", "QD", 55, PRICE))

    def test_the_line_waits_for_less(self):
        line = self.line()
        self.assertEqual(
            [line[name] for name in ("quantity", "confirmed", "backordered",
                                     "status")], ["55", "35", "20", "IQ"])
        self.assertEqual(line["reason"],
                         "Confirmed 35 of 55; 20 to follow on %s" % self.day)

    def test_the_865_says_what_came_off_and_what_still_follows(self):
        self.assertEqual(self.answer(), [
            "45 taken off the backorder at the buyer's request; 20 to follow "
            "on %s" % self.day])

    def test_the_promise_stays(self):
        (promise,) = self.scheduled("backorder")
        self.assertEqual(promise["done_at"], "")

    def test_and_ships_the_smaller_balance_on_the_day(self):
        self.mailbox(ACME, leave=False)
        self.advance()
        despatch = self.document(ACME, "despatch").groups[0].messages[0]
        self.assertEqual([item.get(2) for item in despatch.find_all("SN1")],
                         ["20"])
        invoice = self.document(ACME, "invoice").groups[0].messages[0]
        self.assertEqual([item.get(2) for item in invoice.find_all("IT1")],
                         ["20"])
        self.assertEqual(self.line()["shipped"], "55")

    def test_it_can_be_lowered_again_and_then_cancelled(self):
        self.change(("1", "QD", 40, PRICE))
        self.assertEqual(self.line()["backordered"], "5")
        self.change(("1", "QD", 35, PRICE))
        self.assertEqual(self.line()["backordered"], "0")
        self.advance()
        self.assertEqual(len(self.sent("856")), 1)


class AnythingElseIsStillRefused(ChangeCase):
    """Whole, with nothing in it applied, and told what may be asked."""

    def refused(self, *lines, **kw):
        before = self.order(self.po)["lines"]
        receipt = self.change(*lines, **kw)
        self.assertEqual([item["reason"] for item in receipt["refusals"]],
                         [documents.ONLY_THE_BACKORDER])
        self.assertEqual(self.order(self.po)["lines"], before)
        self.assertEqual(self.mailbox(ACME, "change-response"), [])
        (promise,) = self.scheduled("backorder")
        self.assertEqual(promise["done_at"], "")

    def test_the_reason_says_what_can_still_be_changed(self):
        self.assertTrue(documents.ONLY_THE_BACKORDER.startswith(
            documents.ALREADY_INVOICED))
        self.assertIn("still waiting for stock", documents.ONLY_THE_BACKORDER)

    def test_below_what_has_shipped(self):
        self.refused(("1", "QD", 20, PRICE))

    def test_raising_the_balance(self):
        self.refused(("1", "QI", 150, PRICE))

    def test_a_line_with_nothing_waiting(self):
        self.refused(("2", "QD", 3, "12.50"))

    def test_a_good_line_beside_a_bad_one_takes_neither(self):
        self.refused(("1", "QD", 35, PRICE), ("2", "QD", 3, "12.50"))

    def test_another_price(self):
        self.refused(("1", "QD", 35, "99.00"))

    def test_a_line_added(self):
        self.refused(("3", "AI", 5, "12.50"))

    def test_deleting_a_line_part_of_which_has_shipped(self):
        self.refused(("1", "DI", 0, PRICE))

    def test_cancelling_the_whole_order(self):
        self.refused(purpose="01")

    def test_a_no_change_line_beside_a_good_one_is_fine(self):
        receipt = self.change(("1", "QD", 35, PRICE), ("2", "NC", 5, "12.50"))
        self.assertEqual(receipt.get("refusals") or [], [])
        self.assertEqual(self.line()["backordered"], "0")


class ASellerThatOverShipped(BackorderCase):
    """Found in review: what shipped is the floor, not what was confirmed.

    `over-ship` packs three in ten more than it confirmed (#212): 35
    confirmed, 46 packed and billed. Lowering the line to 35 would leave an
    order for 35 against 46 invoiced - the one thing a change may never do,
    and what the ordinary path refuses in those words.
    """

    def setUp(self):
        super().setUp()
        self.behaviour(ACME, "over-ship")
        self.send(x12_order(self.po, lines=SHORT_ORDER))
        line = self.line()
        self.assertEqual((line["confirmed"], line["shipped"],
                          line["invoiced"]), ("35", "46", "46"))
        self.total = self.order(self.po)["total"]

    def change(self, quantity):
        return self.send(x12_change(self.po, [("1", "QD", quantity, PRICE)],
                                    skus=SKUS))

    def test_it_cannot_be_lowered_to_what_was_confirmed(self):
        receipt = self.change(35)
        self.assertEqual([item["reason"] for item in receipt["refusals"]],
                         [documents.ONLY_THE_BACKORDER])
        self.assertEqual(self.line()["quantity"], "100")

    def test_nor_to_anything_under_what_shipped(self):
        receipt = self.change(40)
        self.assertEqual([item["reason"] for item in receipt["refusals"]],
                         [documents.ONLY_THE_BACKORDER])

    def test_the_total_still_covers_what_was_billed(self):
        self.change(35)
        self.assertEqual(self.order(self.po)["total"], self.total)

    def test_the_balance_above_what_shipped_can_still_come_down(self):
        receipt = self.change(60)
        self.assertEqual(receipt.get("refusals") or [], [])
        self.assertEqual(self.line()["quantity"], "60")


class AShortLineThatIsAlsoRepriced(BackorderCase):
    """Found in review: lowered part-way, the reason keeps the price."""

    def setUp(self):
        super().setUp()
        self.send(x12_order(self.po, lines=((SCARCE, 100, "100.00"),)))
        self.assertIn("priced at 124.50, the order said 100.00",
                      self.line()["reason"])

    def test_part_way_the_price_is_still_named(self):
        self.send(x12_change(self.po, [("1", "QD", 55, "100.00")], skus=SKUS))
        line = self.line()
        self.assertEqual(line["reason"],
                         "Confirmed 35 of 55; 20 to follow on %s; priced at "
                         "124.50, the order said 100.00" % line["scheduled_on"])

    def test_to_what_is_confirmed_it_is_a_price_change_and_says_so(self):
        self.send(x12_change(self.po, [("1", "QD", 35, "100.00")], skus=SKUS))
        line = self.line()
        self.assertEqual((line["status"], line["reason"]),
                         ("IP", "Priced at 124.50, the order said 100.00"))


class AnInvoicedOrderWithNothingWaiting(BackorderCase):

    def test_is_refused_in_the_words_it_always_was(self):
        self.send(x12_order(self.po))
        receipt = self.send(
            x12_change(self.po, [("1", "QD", 10, "12.50")]))
        self.assertEqual([item["reason"] for item in receipt["refusals"]],
                         [documents.ALREADY_INVOICED])

    def test_and_so_is_one_whose_backorder_has_shipped(self):
        self.send(x12_order(self.po, lines=SHORT_ORDER))
        self.advance()
        receipt = self.send(
            x12_change(self.po, [("1", "QD", 35, PRICE)], skus=SKUS))
        self.assertEqual([item["reason"] for item in receipt["refusals"]],
                         [documents.ALREADY_INVOICED])


class ALineWithNoStockAtAll(BackorderCase):
    """An `IB` line beside one that shipped: it can be deleted outright."""

    def setUp(self):
        super().setUp()
        conn = self.httpd.mock.conn
        with self.httpd.mock.lock:
            conn.execute("UPDATE catalog SET in_stock = 0 WHERE sku = ?",
                         (SCARCE,))
            conn.commit()
        self.send(x12_order(self.po, lines=SHORT_ORDER))
        self.assertEqual(self.order(self.po)["status"], "invoiced")

    def test_deleted_it_waits_for_nothing_and_nothing_follows(self):
        receipt = self.send(
            x12_change(self.po, [("1", "DI", 0, PRICE)], skus=SKUS))
        self.assertEqual(receipt.get("refusals") or [], [])
        line = self.line()
        self.assertEqual((line["status"], line["backordered"]), ("IR", "0"))
        self.advance()
        self.assertEqual(len(self.sent("856")), 1)

    def test_lowered_it_ships_the_smaller_quantity_on_the_day(self):
        self.send(x12_change(self.po, [("1", "QD", 40, PRICE)], skus=SKUS))
        self.assertEqual(self.line()["backordered"], "40")
        self.advance()
        self.assertEqual(self.line()["shipped"], "40")


class InEdifact(BackorderCase):

    def test_an_ordchg_cancels_the_balance_and_the_ordrsp_says_so(self):
        self.send(edifact_order(self.po, lines=SHORT_ORDER),
                  headers=EDIFACT_TYPE)
        self.mailbox(EURODIS, leave=False)
        receipt = self.send(
            edifact_change(self.po, [("1", "3", 35, PRICE)], skus=SKUS),
            headers=EDIFACT_TYPE)
        self.assertEqual(receipt.get("refusals") or [], [])
        self.assertEqual(self.line()["backordered"], "0")
        message = self.document(
            EURODIS, "change-response").groups[0].messages[0]
        self.assertIn("65 no longer to follow",
                      " ".join(item.comp(4, 1) for item in
                               message.find_all("FTX")))
        self.advance()
        self.assertEqual(len(self.sent("DESADV", EURODIS)), 1)


if __name__ == "__main__":
    unittest.main()
