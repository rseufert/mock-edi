"""Behaviours for a supplier partner: a buyer that misbehaves (#127).

A supplier's integration meeting a buyer that always behaves is only half a
test. These are the three the mock did not have, all of them about an order
it placed, and so refused on a customer:

* `duplicate-order` sends the 850 twice under one number - the mirror of
  `duplicate-invoice`, and the reason a supplier needs #44's duplicate rule
* `change-after-confirm` sends an 860 the moment the 855 lands
* `cancel-late` sends an 860 cancelling the moment the 856 does, which is a
  cancellation after the goods have left

Nothing here waits on a clock. The last two are promised when the supplier's
document arrives and kept through the `scheduled` table, so `/_mock/advance`
releases them and no test sleeps.

A behaviour changes what the buyer *sends*, not what it notices: the last
class holds `cancel-late` to #126's judgement of the invoice that follows.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import db, schema

from support import ACME, MockServerCase
from test_reconciliation import AS_ORDERED
from test_three_way_match import MatchCase, item
from test_two_mocks import Pair

LINE = [{"sku": "WIDGET-001", "quantity": "100", "uom": "EA", "price": "12.50"}]


class BuyerCase(MatchCase):
    def behaving(self, name):
        status, _h, data = self.patch("/_mock/partners/" + self.partner,
                                      {"behaviour": name})
        self.assertEqual(status, 200, data)
        self.mailbox(self.partner, leave=False)

    def sent(self):
        return [row["code"] for row in self.mailbox(self.partner)
                if row["kind"] != "acknowledgment"]

    def code(self, kind):
        """What this dialect calls a kind: 850/860, or ORDERS/ORDCHG."""
        return schema.set_code(self.dialect, kind)

    def place(self, po_number="PO-D", lines=None):
        status, _h, data = self.post("/_mock/purchase", {
            "partner": self.partner, "po_number": po_number,
            "lines": lines if lines is not None else LINE})
        self.assertEqual(status, 201, data)
        return data


class TheRecord(unittest.TestCase):
    """The three are supplier-only, which is all the refusal needs (#142)."""

    def test_each_is_described_and_given_a_role(self):
        for name in ("duplicate-order", "change-after-confirm", "cancel-late"):
            self.assertIn(name, db.BEHAVIOURS, name)
            self.assertEqual(db.BEHAVIOUR_ROLES[name], db.SUPPLIER_ONLY, name)

    def test_and_every_behaviour_has_both(self):
        self.assertEqual(set(db.BEHAVIOURS), set(db.BEHAVIOUR_ROLES))


class WhichPartnersMayHaveThem(MockServerCase):
    def test_a_customer_is_refused_with_the_reason(self):
        for name in ("duplicate-order", "change-after-confirm", "cancel-late"):
            status, _h, data = self.patch("/_mock/partners/" + ACME,
                                          {"behaviour": name})
            self.assertEqual(status, 400, data)
            # #130's refusal, reading the junior's record in the other
            # direction, with no new code (#142).
            self.assertIn(name, data["error"])
            self.assertIn("A customer may be", data["error"])
            self.assertNotIn(name, data["error"].split("A customer may be")[1])

    def test_and_a_supplier_may_have_each(self):
        for name in ("duplicate-order", "change-after-confirm", "cancel-late"):
            status, _h, data = self.patch("/_mock/partners/NORTHWIND",
                                          {"behaviour": name})
            self.assertEqual(status, 200, data)
            self.assertEqual(data["behaviour"], name)


class DuplicateOrder(BuyerCase):
    def test_the_order_goes_out_twice_under_one_number(self):
        self.behaving("duplicate-order")
        self.place()
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent(), [self.code(schema.ORDER)] * 2)

    def test_the_second_says_what_it_is(self):
        self.behaving("duplicate-order")
        self.place()
        self.post("/_mock/advance?all")
        self.assertEqual([row["note"] for row in self.mailbox(self.partner)
                          if row["code"] == self.code(schema.ORDER)],
                         ["", "duplicate of the order above"])

    def test_and_the_mock_still_holds_one_order(self):
        # The buyer misbehaves on the wire; its own books stay straight.
        self.behaving("duplicate-order")
        self.place()
        self.post("/_mock/advance?all")
        _s, _h, orders = self.get("/_mock/orders")
        self.assertEqual([o["po_number"] for o in orders if o["po_number"] == "PO-D"],
                         ["PO-D"])

    def duplicated(self):
        pair = Pair()
        self.addCleanup(pair.close)
        pair.buyer.expect("PATCH", "/_mock/partners/SELLCO",
                          {"behaviour": "duplicate-order"}, status=200)
        pair.place("PO-2M")
        pair.exchange()
        return pair, pair.buyer.get("/_mock/orders/PO-2M")[2]

    def test_a_real_supplier_fulfils_it_once(self):
        """Against a real seller, which refuses the second one (#165).

        This is what the behaviour is for, and when it was written the seller
        failed it: #44 refuses a replayed *interchange*, and the second order
        is a fresh interchange restating the same PO number, so it was
        answered, shipped and billed all over again. The seller now refuses a
        number it has already acted on, with an 855 - which is the fourth
        document below.
        """
        pair, order = self.duplicated()
        self.assertEqual(pair.received(), ["855", "856", "810", "855"])
        self.assertEqual([(row["ordered"], row["shipped"], row["billed"])
                          for row in order["reconciliation"]],
                         [("100", "100", "100"), ("40", "40", "40")])

    def test_and_the_refusal_does_not_un_confirm_what_already_shipped(self):
        """#168: the refusal is an 855 on the same number, so it is the
        latest answer, and it says every line is refused. It cannot take back
        what an earlier answer confirmed and the supplier shipped against.
        """
        _pair, order = self.duplicated()
        self.assertEqual([row["confirmed"] for row in order["reconciliation"]],
                         ["100", "40"])

    def test_though_the_buyer_still_reports_what_the_refusal_said(self):
        """The floor corrects the running total, not the record."""
        _pair, order = self.duplicated()
        self.assertEqual({d["rule"] for d in order["disagreements"]},
                         {"confirmed-less"})
        self.assertIn("order number already in use",
                      order["disagreements"][0]["note"])


class ChangeAfterConfirm(BuyerCase):
    def test_an_860_follows_the_855(self):
        self.behaving("change-after-confirm")
        self.confirmed()
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent(), [self.code(schema.CHANGE)])

    def test_it_lowers_the_line_it_names(self):
        self.behaving("change-after-confirm")
        self.confirmed()
        self.post("/_mock/advance?all")
        self.assertEqual(self.order("PO-B")["lines"][0]["quantity"], "50")

    def test_nothing_is_sent_before_the_855_arrives(self):
        self.behaving("change-after-confirm")
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent(), [])

    def test_a_second_855_does_not_earn_a_second_change(self):
        # A supplier correcting its confirmation has not been changed at twice.
        self.behaving("change-after-confirm")
        self.confirmed()
        self.confirmed()
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent(), [self.code(schema.CHANGE)])

    def test_it_is_promised_rather_than_sent_on_a_clock(self):
        self.behaving("change-after-confirm")
        self.confirmed()
        _s, _h, data = self.get("/_mock/orders/PO-B/timeline")
        self.assertIn("buyer-change",
                      [e.get("kind") for e in data["events"]
                       if e["event"] == "promised"])


class CancelLate(BuyerCase):
    def test_an_860_cancelling_follows_the_856(self):
        self.behaving("cancel-late")
        self.confirmed()
        self.shipped()
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent(), [self.code(schema.CHANGE)])

    def test_and_the_order_is_cancelled_after_the_goods_left(self):
        self.behaving("cancel-late")
        self.confirmed()
        self.shipped()
        self.post("/_mock/advance?all")
        order = self.order("PO-B")
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order["reconciliation"][0]["shipped"], "100")

    def test_the_855_alone_is_not_enough(self):
        self.behaving("cancel-late")
        self.confirmed()
        self.post("/_mock/advance?all")
        self.assertEqual(self.sent(), [])

    def test_an_invoice_after_it_is_a_disagreement(self):
        # The reason this piece waited for #126: a behaviour changes what the
        # buyer sends, not what it notices. #152's rule does the noticing.
        self.behaving("cancel-late")
        self.confirmed()
        self.shipped()
        self.post("/_mock/advance?all")
        summary = self.billed()
        self.assertIn("billed-cancelled",
                      [d["rule"] for d in summary["disagreements"]])


class WhatTheyWriteIsValid(BuyerCase):
    """The project's own property, for the documents these three produce.

    `test_dictionary.GeneratedDocumentsAreValid` holds every behaviour a
    customer may have to it, and cannot reach these: it drives the seller
    flow from an inbound order, and these belong to a supplier. So the same
    check, from the buying side.
    """

    def valid(self):
        from support import parse
        from mockedi import validate
        for row in self.mailbox(self.partner):
            report = validate.validate(parse(row["payload"]))
            self.assertEqual([f.note for m in report.messages
                              for f in m.findings], [],
                             "%s does not validate" % row["code"])
            self.assertTrue(report.messages, row["code"])

    def test_the_orders_duplicate_order_writes(self):
        self.behaving("duplicate-order")
        self.place()
        self.post("/_mock/advance?all")
        self.valid()

    def test_the_change_after_confirm_writes(self):
        self.behaving("change-after-confirm")
        self.confirmed()
        self.post("/_mock/advance?all")
        self.valid()

    def test_the_cancellation_cancel_late_writes(self):
        self.behaving("cancel-late")
        self.confirmed()
        self.shipped()
        self.post("/_mock/advance?all")
        self.valid()


class WhatTheyWriteIsValidInEdifact(WhatTheyWriteIsValid):
    dialect = "EDIFACT"


class InEdifact(ChangeAfterConfirm):
    dialect = "EDIFACT"


class CancelLateInEdifact(CancelLate):
    dialect = "EDIFACT"


if __name__ == "__main__":
    unittest.main()
