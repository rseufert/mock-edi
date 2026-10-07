"""The balance of a line confirmed short of stock ships on its date (#335).

An order for more than the catalogue has in stock was confirmed for what
there was, with a date on the line and the reason "the balance is not
available", and the balance was never heard of again. The filed fault was
about `IB`, a line with no stock at all - which a running mock cannot
produce, because no seeded item is out of stock and nothing draws stock
down. The promise a caller could actually see broken was this one.

So the balance is backordered: recorded on the line, promised on the
schedule for the line's date, and on that date confirmed, packed and billed
as a second consignment with a shipment number, an 856, an invoice number
and an 810 of its own. `short-ship` is not this: that partner confirms less
than it was asked for on purpose, promises no balance, and sends none.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import db, documents, partners, transactions

from support import (ACME, EURODIS, edifact_order, parse, x12_change,
                     x12_order)
from test_document_numbers import TimelineCase, events

EDIFACT_TYPE = {"Content-Type": "application/edifact"}

# PANEL-A3 is the seeded catalogue's scarcest item: 35 in stock.
SCARCE, IN_STOCK, PRICE = "PANEL-A3", 35, "124.50"
SHORT_ORDER = ((SCARCE, 100, PRICE), ("WIDGET-001", 5, "12.50"))


class BackorderCase(TimelineCase):
    po = "PO-BACK"

    def scheduled(self, kind=None, everything=True):
        _status, _headers, rows = self.get(
            "/_mock/scheduled" + ("?all" if everything else ""))
        return [row for row in rows if row["po_number"] == self.po
                and (kind is None or row["kind"] == kind)]

    def advance(self, query="all"):
        status, _headers, found = self.post("/_mock/advance?" + query)
        self.assertEqual(status, 200, found)
        return found

    def sent(self, code, partner=ACME):
        return events(self.timeline(self.po, partner), "sent", code)

    def line(self, number="1"):
        return [row for row in self.order(self.po)["lines"]
                if row["line"] == number][0]


class TheBalanceIsPromised(BackorderCase):
    """What the buyer is told, and what the schedule holds, on the day."""

    def setUp(self):
        super().setUp()
        self.send(x12_order(self.po, lines=SHORT_ORDER))

    def test_the_line_records_what_it_is_waiting_for(self):
        line = self.line()
        self.assertEqual((line["status"], line["confirmed"],
                          line["backordered"]), ("IQ", "35", "65"))

    def test_a_line_with_stock_waits_for_nothing(self):
        self.assertEqual(self.line("2")["backordered"], "0")

    def test_the_reason_says_how_many_follow_and_when(self):
        line = self.line()
        self.assertEqual(line["reason"], "Confirmed 35 of 100; 65 to follow "
                                         "on %s" % line["scheduled_on"])

    def test_the_855_carries_the_date_and_the_reason(self):
        message = self.document(ACME, "response").groups[0].messages[0]
        ack = message.find_all("ACK")[0]
        date = self.line()["scheduled_on"].replace("-", "")
        self.assertEqual([ack.get(n) for n in (1, 2, 5)], ["IQ", "35", date])
        self.assertIn("65 to follow on",
                      [ref.get(3) for ref in message.find_all("REF")
                       if ref.get(1) == "ZZ"][0])

    def test_one_backorder_is_promised_for_the_start_of_that_day(self):
        (promise,) = self.scheduled("backorder")
        due = datetime.datetime.strptime(
            promise["due_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc).astimezone()
        self.assertEqual(due.date().isoformat(), self.line()["scheduled_on"])
        self.assertEqual(due.time(), datetime.time.min)
        self.assertEqual(promise["done_at"], "")

    def test_only_what_is_in_stock_ships_and_is_billed_today(self):
        (despatch,) = self.sent("856")
        (invoice,) = self.sent("810")
        self.assertEqual(self.line()["shipped"], "35")
        self.assertEqual(self.line()["invoiced"], "35")
        self.assertTrue(despatch["shipment"] and invoice["invoice"])

    def test_a_minute_later_nothing_more_has_shipped(self):
        self.advance("seconds=60")
        self.assertEqual(len(self.sent("856")), 1)
        self.assertEqual(self.line()["backordered"], "65")


class TheBalanceShips(BackorderCase):
    """The date comes: a second consignment, and nothing like the first."""

    def setUp(self):
        super().setUp()
        self.send(x12_order(self.po, lines=SHORT_ORDER))
        self.scheduled_on = self.line()["scheduled_on"]
        self.advanced = self.advance()

    def test_the_clock_went_to_the_date_and_no_further(self):
        status, _headers, state = self.get("/_mock/state")
        now = datetime.datetime.fromisoformat(state["clock"]["now"]).astimezone()
        self.assertEqual(now.date().isoformat(), self.scheduled_on)

    def test_the_line_is_whole_and_waits_for_nothing(self):
        line = self.line()
        self.assertEqual(
            [line[name] for name in ("status", "confirmed", "shipped",
                                     "invoiced", "backordered", "reason")],
            ["IA", "100", "100", "100", "0", ""])

    def test_a_second_856_and_a_second_810_were_sent(self):
        self.assertEqual(len(self.sent("856")), 2)
        self.assertEqual(len(self.sent("810")), 2)

    def test_each_names_a_shipment_of_its_own(self):
        first, second = self.sent("856")
        self.assertNotEqual(first["shipment"], second["shipment"])

    def test_each_invoice_has_a_number_of_its_own_and_bills_its_shipment(self):
        despatches = [item["shipment"] for item in self.sent("856")]
        first, second = self.sent("810")
        self.assertNotEqual(first["invoice"], second["invoice"])
        self.assertEqual([first["shipment"], second["shipment"]], despatches)

    def test_the_second_consignment_carries_the_balance_and_only_that(self):
        first, second = [parse(row["payload"]).groups[0].messages[0]
                         for row in self.mailbox(ACME, "despatch")]
        self.assertEqual([item.get(2) for item in first.find_all("SN1")],
                         ["35", "5"])
        self.assertEqual([item.get(2) for item in second.find_all("SN1")],
                         ["65"])
        bills = [parse(row["payload"]).groups[0].messages[0]
                 for row in self.mailbox(ACME, "invoice")]
        self.assertEqual([[item.get(2) for item in bill.find_all("IT1")]
                          for bill in bills], [["35", "5"], ["65"]])

    def test_it_is_dated_the_day_it_shipped(self):
        first, second = self.order(self.po)["shipments"]
        self.assertEqual(second["shipped_on"], self.scheduled_on)
        self.assertLess(first["shipped_on"], second["shipped_on"])

    def test_the_two_invoices_come_to_the_whole_order(self):
        totals = [invoice["total"] for invoice in self.order(self.po)["invoices"]]
        # 35 x 124.50 + 5 x 12.50, then 65 x 124.50.
        self.assertEqual(totals, ["4420.00", "8092.50"])

    def test_the_backorder_is_kept_and_the_work_it_made_is_too(self):
        self.assertEqual([row["kind"] for row in self.scheduled()
                          if row["done_at"] == ""], [])
        self.assertEqual([row["kind"] for row in self.scheduled()],
                         ["despatch", "invoice", "backorder", "despatch",
                          "invoice"])

    def test_each_packing_names_the_promise_it_kept(self):
        promised = {row["id"]: row["kind"] for row in self.scheduled()}
        packed = events(self.timeline(self.po), "packed")
        self.assertEqual(len({item["promise"] for item in packed}), 2)
        self.assertEqual({promised[item["promise"]] for item in packed},
                         {"despatch"})

    def test_advancing_again_sends_nothing_more(self):
        self.advance()
        self.assertEqual(len(self.sent("856")), 2)
        self.assertEqual(len(self.sent("810")), 2)


class InEdifact(BackorderCase):

    def setUp(self):
        super().setUp()
        self.send(edifact_order(self.po, lines=SHORT_ORDER),
                  headers=EDIFACT_TYPE)

    def test_the_ordrsp_says_65_are_backordered_and_why(self):
        message = self.document(EURODIS, "response").groups[0].messages[0]
        self.assertEqual([q.comp(1, 2) for q in message.find_all("QTY")
                          if q.comp(1, 1) == "83"], ["65"])
        self.assertIn("65 to follow on", message.find("FTX").comp(4, 1))

    def test_a_second_desadv_and_invoic_follow_on_the_date(self):
        self.assertEqual(len(self.sent("DESADV", EURODIS)), 1)
        self.advance()
        despatches = self.sent("DESADV", EURODIS)
        invoices = self.sent("INVOIC", EURODIS)
        self.assertEqual((len(despatches), len(invoices)), (2, 2))
        self.assertEqual(len({item["shipment"] for item in despatches}), 2)
        self.assertEqual(len({item["invoice"] for item in invoices}), 2)


class NoStockAtAll(BackorderCase):
    """`IB`: the same thing with a first consignment of nothing.

    No seeded item is out of stock, so the stock is taken away here. The
    order waits; it is not refused.
    """

    def setUp(self):
        super().setUp()
        conn = self.httpd.mock.conn
        with self.httpd.mock.lock:
            conn.execute("UPDATE catalog SET in_stock = 0 WHERE sku = ?",
                         (SCARCE,))
            conn.commit()
        self.send(x12_order(self.po, lines=((SCARCE, 10, PRICE),)))

    def test_the_line_is_backordered_whole(self):
        line = self.line()
        self.assertEqual((line["status"], line["confirmed"],
                          line["backordered"]), ("IB", "0", "10"))
        self.assertEqual(line["reason"], "%s is out of stock; 10 to follow on "
                                         "%s" % (SCARCE, line["scheduled_on"]))

    def test_the_order_waits_and_is_not_refused(self):
        self.assertEqual(self.order(self.po)["status"], "received")
        self.assertEqual(self.sent("856"), [])
        self.assertEqual(self.sent("810"), [])

    def test_all_of_it_ships_on_the_date(self):
        self.advance()
        line = self.line()
        self.assertEqual((line["status"], line["shipped"], line["invoiced"]),
                         ("IA", "10", "10"))
        self.assertEqual((len(self.sent("856")), len(self.sent("810"))), (1, 1))
        self.assertEqual(self.order(self.po)["status"], "invoiced")


class TwoDates(BackorderCase):
    """Two lines due on two days are two promises, kept on their own days."""

    def setUp(self):
        super().setUp()
        conn = self.httpd.mock.conn
        with self.httpd.mock.lock:
            conn.execute("UPDATE catalog SET lead_days = 2 WHERE sku = ?",
                         (SCARCE,))
            conn.execute("UPDATE catalog SET lead_days = 5, in_stock = 60"
                         " WHERE sku = 'GEAR-200'")
            conn.commit()
        self.send(x12_order(self.po, lines=((SCARCE, 100, PRICE),
                                            ("GEAR-200", 100, "14.25"))))

    def test_one_promise_for_each_day(self):
        days = sorted({row["scheduled_on"]
                       for row in self.order(self.po)["lines"]})
        self.assertEqual(len(days), 2)
        self.assertEqual(len(self.scheduled("backorder")), 2)

    def test_the_first_day_releases_only_its_own_line(self):
        self.advance("seconds=%d" % (3 * 86400))
        self.assertEqual(self.line("1")["backordered"], "0")
        self.assertEqual(self.line("2")["backordered"], "40")
        self.assertEqual(len(self.sent("856")), 2)

    def test_and_the_second_day_the_other(self):
        self.advance()
        self.assertEqual(self.line("2")["shipped"], "100")
        self.assertEqual(len(self.sent("856")), 3)
        self.assertEqual(len(self.sent("810")), 3)


class AChangeWhileItWaits(BackorderCase):
    """An 860 during the wait is packed promptly, not on the backorder's day.

    The reason the backorder is a promise of its own kind: were it a
    despatch waiting days ahead, the change would find work already
    scheduled and promise nothing.
    """
    # Invoiced an hour after despatch, so the order can still be changed.
    config_kwargs = {"invoice_delay_ms": 3600 * 1000}

    def setUp(self):
        super().setUp()
        self.send(x12_order(self.po, lines=SHORT_ORDER))
        self.send(x12_change(self.po, [("2", "QI", 10, "12.50")]))

    def test_the_raised_quantity_ships_at_once(self):
        self.assertEqual(len(self.sent("856")), 2)
        self.assertEqual(self.line("2")["shipped"], "10")

    def test_the_backorder_still_waits(self):
        (promise,) = self.scheduled("backorder")
        self.assertEqual(promise["done_at"], "")
        self.assertEqual(self.line("1")["backordered"], "65")

    def test_and_ships_on_its_day_as_a_third_consignment(self):
        self.advance()
        self.assertEqual(len(self.sent("856")), 3)
        self.assertEqual(self.line("1")["shipped"], "100")
        billed = sum(int(row["invoiced"]) for row in self.order(self.po)["lines"])
        self.assertEqual(billed, 110)


class Withdrawn(BackorderCase):
    """An order restated or cancelled before it ships takes the promise too."""
    config_kwargs = {"despatch_delay_ms": 3600 * 1000,
                     "invoice_delay_ms": 3600 * 1000}

    def setUp(self):
        super().setUp()
        self.send(x12_order(self.po, lines=SHORT_ORDER))

    def test_restated_within_stock_nothing_waits(self):
        self.send(x12_order(self.po, lines=((SCARCE, 10, PRICE),)))
        (promise,) = self.scheduled("backorder")
        self.assertNotEqual(promise["done_at"], "")
        self.assertEqual(promise["note"], "order restated")
        self.advance()
        self.assertEqual(len(self.sent("856")), 1)

    def test_cancelled_nothing_ships_then_or_later(self):
        self.send(x12_change(self.po, [], purpose="01"))
        (promise,) = self.scheduled("backorder")
        self.assertEqual(promise["note"], "order cancelled")
        self.assertEqual(self.line()["backordered"], "0")
        self.advance()
        self.assertEqual(self.sent("856"), [])

    def test_a_line_lowered_to_what_is_in_stock_waits_no_longer(self):
        self.send(x12_change(self.po, [("1", "QD", 20, PRICE)]))
        self.assertEqual(self.line()["backordered"], "0")
        (promise,) = self.scheduled("backorder")
        self.assertEqual(promise["note"],
                         "nothing is waiting for stock that day")


class ShortShipIsNotABackorder(BackorderCase):
    """A partner that under-confirms on purpose promises no balance."""

    def setUp(self):
        super().setUp()
        self.behaviour(ACME, "short-ship")
        self.send(x12_order(self.po, lines=(("WIDGET-001", 100, "12.50"),)))

    def test_nothing_is_waited_for_and_the_reason_is_what_it_was(self):
        line = self.line()
        self.assertEqual((line["confirmed"], line["backordered"]), ("80", "0"))
        self.assertEqual(line["reason"],
                         "Confirmed 80 of 100; the balance is not available")

    def test_nothing_is_promised_and_nothing_follows(self):
        self.assertEqual(self.scheduled("backorder"), [])
        self.advance()
        self.assertEqual(len(self.sent("856")), 1)

    def test_not_even_where_stock_is_short_as_well(self):
        self.po = "PO-BACK-2"
        self.send(x12_order(self.po, lines=((SCARCE, 100, PRICE),)))
        self.assertEqual(self.line()["backordered"], "0")
        self.assertEqual(self.scheduled("backorder"), [])


class AnOrdinaryOrder(BackorderCase):

    def test_promises_no_backorder(self):
        self.send(x12_order(self.po))
        self.assertEqual(self.scheduled("backorder"), [])
        self.assertEqual([row["backordered"]
                          for row in self.order(self.po)["lines"]], ["0", "0"])


class Deciding(unittest.TestCase):
    """`decide` and `release_backorders`, without a server."""

    def setUp(self):
        self.conn = db.connect()
        db.seed(self.conn)
        self.partner = partners.get(self.conn, ACME)
        self.when = datetime.datetime(2026, 10, 7, 12, tzinfo=datetime.timezone.utc)

    def tearDown(self):
        self.conn.close()

    def order(self, *lines):
        order = transactions.Order(po_number="PO-D", currency="USD")
        for index, (sku, quantity) in enumerate(lines, 1):
            order.lines.append(transactions.Line(
                number=str(index), sku=sku, quantity=transactions.number(
                    str(quantity)), uom="EA",
                price=transactions.number("0.00")))
        return order

    def test_every_decision_says_what_is_backordered(self):
        decisions = documents.decide(
            self.conn, self.partner,
            self.order((SCARCE, 100), ("WIDGET-001", 5), ("NO-SUCH", 1),
                       (SCARCE, 0)), self.when)
        self.assertEqual([str(decision[5]) for decision in decisions],
                         ["65", "0", "0", "0"])

    def test_release_takes_only_what_is_due_by_the_day(self):
        order = self.order((SCARCE, 100))
        documents.record_order(self.conn, self.partner, order, self.when)
        (day,) = documents.backorder_dates(self.conn, "PO-D", ACME)
        due = datetime.date.fromisoformat(day)
        early = documents.release_backorders(
            self.conn, "PO-D", ACME, due - datetime.timedelta(days=1))
        self.assertEqual(early, [])
        self.assertEqual(documents.release_backorders(
            self.conn, "PO-D", ACME, due), ["1"])
        self.assertEqual(documents.backorder_dates(self.conn, "PO-D", ACME), [])

    def test_a_released_line_the_seller_prices_differently_says_so(self):
        order = self.order((SCARCE, 100))
        order.lines[0].price = transactions.number("100.00")
        documents.record_order(self.conn, self.partner, order, self.when)
        (day,) = documents.backorder_dates(self.conn, "PO-D", ACME)
        documents.release_backorders(self.conn, "PO-D", ACME,
                                     datetime.date.fromisoformat(day))
        (line,) = documents.order_lines(self.conn, "PO-D", ACME)
        self.assertEqual(line["status"], "IP")
        self.assertIn("the order said", line["reason"])


if __name__ == "__main__":
    unittest.main()
