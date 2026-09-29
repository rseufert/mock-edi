"""Two partners, one purchase order number: two orders.

Real purchase order numbers are unique per buyer, not to the world, and two
customers both sending a 4500000042 is ordinary. On 0.4.0 the mock kept orders
by number alone, so a partner naming a number it did not own could act on
someone else's order: an 860 cancelled a stranger's order, and an ordinary 850
replaced one outright. 0.5.0 (#131) refused a number somebody else held, which
was safe and wrong in principle. Orders are now keyed by partner and number
(#132), so the second customer's order is simply its own.

Every test here asserts on the *first* customer's order as much as on the
second's: an answer that looked right while corrupting the other order would
pass a test that only read the receipt.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, documents, validate

from support import (ACME, EURODIS, GLOBEX, INITECH, MockServerCase,
                     edifact_change, edifact_order, parse, x12_change, x12_order)

EDIFACT = {"Content-Type": "application/edifact"}
# A window, so the order is still changeable when the second partner tries.
WINDOW = {"despatch_delay_ms": 3600 * 1000, "invoice_delay_ms": 3600 * 1000}

PO = "4500000042"


class TheAnswerToAStranger(unittest.TestCase):
    def test_does_not_name_the_other_partner(self):
        # A partner changing a number it does not hold is told what it would
        # be told for any unknown order. It has no business learning which
        # numbers its competitors use.
        self.assertEqual(documents.NOT_FOUND, "no such purchase order")


class OwnedCase(MockServerCase):
    config_kwargs = WINDOW

    def order_of(self, partner):
        return self.order("%s?partner=%s" % (PO, partner))

    def lines(self, order):
        return [(row["line"], row["sku"], row["quantity"], row["confirmed"],
                 row["status"]) for row in order["lines"]]


class AStrangersChange(OwnedCase):
    """A change to a number the sender holds no order under."""

    def setUp(self):
        super().setUp()
        self.send(x12_order(PO, sender=ACME))
        self.before = self.order_of(ACME)

    def unchanged(self):
        after = self.order_of(ACME)
        self.assertEqual(after["status"], self.before["status"])
        self.assertEqual(self.lines(after), self.lines(self.before))

    def test_a_change_from_another_partner_is_refused(self):
        summary = self.send(x12_change(PO, lines=[("1", "QD", 5, "12.50")],
                                       sender=INITECH))
        self.assertEqual(summary["changed"], [])
        self.assertEqual(summary["refusals"],
                         [{"order": PO, "reason": documents.NOT_FOUND}])
        self.unchanged()

    def test_a_cancellation_from_another_partner_is_refused(self):
        summary = self.send(x12_change(PO, lines=[("1", "CA", 0, "0")],
                                       purpose="01", sender=INITECH))
        self.assertEqual(summary["refusals"][0]["reason"], documents.NOT_FOUND)
        self.unchanged()

    def test_a_restated_850_from_another_partner_is_its_own_order(self):
        # BEG01 = 04 restates an order. INITECH holds none with this number,
        # so it is read as INITECH placing one - as it would be for any
        # number nobody holds - and ACME's is not touched.
        summary = self.send(x12_order(PO, sender=INITECH, purpose="04",
                                      lines=(("WIDGET-001", 1, "12.50"),)))
        self.assertEqual(summary["orders"], [PO])
        self.assertEqual(self.order_of(INITECH)["lines"][0]["quantity"], "1")
        self.unchanged()

    def test_no_865_is_sent_for_a_change_that_was_refused(self):
        self.send(x12_change(PO, lines=[("1", "QD", 5, "12.50")],
                             sender=INITECH))
        self.assertEqual([row["code"] for row in self.mailbox(INITECH)], ["997"])

    def test_the_owner_can_still_change_it(self):
        summary = self.send(x12_change(PO, lines=[("1", "QD", 60, "12.50")],
                                       sender=ACME))
        self.assertEqual(summary["changed"], [PO])
        self.assertEqual(summary["refusals"], [])
        self.assertEqual(self.order_of(ACME)["lines"][0]["confirmed"], "60")

    def test_the_owner_can_still_cancel_it(self):
        self.send(x12_change(PO, lines=[("1", "CA", 0, "0")], purpose="01",
                             sender=ACME))
        self.assertEqual(self.order_of(ACME)["status"], "cancelled")


class TwoCustomersOneNumber(OwnedCase):
    """An ordinary 850 reusing a number somebody else holds."""

    def setUp(self):
        super().setUp()
        self.send(x12_order(PO, sender=ACME))
        self.acme = self.order_of(ACME)

    def scheduled(self):
        _status, _headers, rows = self.get("/_mock/scheduled?all")
        return sorted((row["partner"], row["po_number"], row["kind"])
                      for row in rows)

    def second(self):
        return self.send(x12_order(PO, sender=GLOBEX,
                                   lines=(("BRKT-050", 7, "4.15"),)))

    def test_it_is_recorded(self):
        summary = self.second()
        self.assertEqual(summary["orders"], [PO])
        self.assertEqual(summary["refusals"], [])
        globex = self.order_of(GLOBEX)
        self.assertEqual([row["sku"] for row in globex["lines"]], ["BRKT-050"])

    def test_the_first_order_is_untouched(self):
        self.second()
        self.assertEqual(self.lines(self.order_of(ACME)), self.lines(self.acme))

    def test_each_has_its_own_work_promised(self):
        before = self.scheduled()
        self.second()
        after = self.scheduled()
        self.assertEqual([row for row in after if row[0] == ACME], before)
        self.assertEqual([row for row in after if row[0] == GLOBEX],
                         [(GLOBEX, PO, "despatch"), (GLOBEX, PO, "invoice")])

    def test_the_second_is_answered_as_any_order_is(self):
        self.second()
        message = self.document(GLOBEX, "response").groups[0].messages[0]
        self.assertNotEqual(message.find("BAK").get(2), "RD")
        # Decided under GLOBEX's own behaviour, short-ship: 6 of the 7.
        self.assertEqual([(item.get(1), item.get(2)) for item in message.segments
                          if item.tag == "ACK"], [("IQ", "6")])

    def test_neither_ships_or_bills_the_others_goods(self):
        self.second()
        self.post("/_mock/advance?all")
        for partner, skus in ((ACME, {"WIDGET-001", "BRKT-050"}),
                              (GLOBEX, {"BRKT-050"})):
            order = self.order_of(partner)
            self.assertEqual({row["sku"] for row in order["lines"]}, skus)
            self.assertEqual([s["partner"] for s in order["shipments"]], [partner])
            self.assertEqual([i["partner"] for i in order["invoices"]], [partner])
        self.assertEqual(self.order_of(GLOBEX)["lines"][0]["shipped"], "6")
        self.assertEqual(self.order_of(ACME)["lines"][0]["shipped"], "100")

    def test_each_timeline_is_its_own(self):
        self.second()
        self.post("/_mock/advance?all")
        for partner in (ACME, GLOBEX):
            _s, _h, data = self.get("/_mock/orders/%s/timeline?partner=%s"
                                    % (PO, partner))
            self.assertEqual(data["partner"], partner)
            codes = [e["code"] for e in data["events"] if e["event"] == "sent"]
            self.assertEqual(codes, ["997", "855", "856", "810"], partner)

    def test_the_number_alone_is_ambiguous_and_says_whose(self):
        self.second()
        for path in ("/_mock/orders/%s" % PO, "/_mock/orders/%s/timeline" % PO):
            status, _h, data = self.get(path)
            self.assertEqual(status, 409, data)
            self.assertEqual(data["partners"], [ACME, GLOBEX])
            self.assertIn("?partner=", data["error"])

    def test_the_list_has_both(self):
        self.second()
        _s, _h, rows = self.get("/_mock/orders")
        self.assertEqual(sorted(row["partner"] for row in rows
                                if row["po_number"] == PO), [ACME, GLOBEX])

    def test_the_shipped_client_takes_the_partner_too(self):
        self.second()
        self.assertEqual(self.mock.order(PO, partner=GLOBEX)["partner"], GLOBEX)
        self.assertEqual(self.mock.timeline(PO, raw=True, partner=ACME)["partner"],
                         ACME)

    def test_a_partner_that_holds_none_is_a_404(self):
        status, _h, data = self.get("/_mock/orders/%s?partner=%s" % (PO, GLOBEX))
        self.assertEqual(status, 404)
        self.assertIn("with GLOBEX", data["error"])

    def test_a_change_reaches_only_the_senders_order(self):
        self.second()
        self.send(x12_change(PO, lines=[("1", "QD", 3, "4.15")], sender=GLOBEX))
        self.assertEqual(self.order_of(GLOBEX)["lines"][0]["quantity"], "3")
        self.assertEqual(self.lines(self.order_of(ACME)), self.lines(self.acme))

    def test_the_same_partner_may_still_replace_its_own(self):
        summary = self.send(x12_order(PO, sender=ACME,
                                      lines=(("GEAR-100", 3, "8.90"),)))
        self.assertEqual(summary["orders"], [PO])
        self.assertEqual([row["sku"] for row in self.order_of(ACME)["lines"]],
                         ["GEAR-100"])

    def test_the_second_855_validates(self):
        self.second()
        row = [item for item in self.mailbox(GLOBEX) if item["code"] == "855"][0]
        report = validate.validate(parse(row["payload"]))
        self.assertTrue(report.clean, "\n".join(ack.explain(report)))


class TheSameInEdifact(OwnedCase):
    OTHER = "NORDIC"

    def setUp(self):
        super().setUp()
        status, _headers, data = self.post(
            "/_mock/partners", {"id": self.OTHER, "name": "Nordic Handel AB",
                                "dialect": "EDIFACT", "qualifier": "ZZ"})
        self.assertEqual(status, 201, data)
        self.send(edifact_order(PO, sender=EURODIS), headers=EDIFACT)

    def test_an_ordchg_from_another_partner_is_refused(self):
        summary = self.send(edifact_change(PO, lines=[("1", "3", 5, "12.50")],
                                           sender=self.OTHER), headers=EDIFACT)
        self.assertEqual(summary["changed"], [])
        self.assertEqual(summary["refusals"][0]["reason"], documents.NOT_FOUND)
        self.assertEqual(self.order_of(EURODIS)["lines"][0]["confirmed"], "100")

    def test_an_orders_reusing_the_number_is_its_own_order(self):
        summary = self.send(edifact_order(PO, sender=self.OTHER,
                                          lines=(("BRKT-050", 7, "4.15"),)),
                            headers=EDIFACT)
        self.assertEqual(summary["orders"], [PO])
        self.assertEqual(self.order_of(self.OTHER)["lines"][0]["sku"], "BRKT-050")
        self.assertEqual(self.order_of(EURODIS)["lines"][0]["quantity"], "100")

    def test_the_owner_can_still_change_it(self):
        summary = self.send(edifact_change(PO, lines=[("1", "3", 60, "12.50")],
                                           sender=EURODIS), headers=EDIFACT)
        self.assertEqual(summary["changed"], [PO])


if __name__ == "__main__":
    unittest.main()
