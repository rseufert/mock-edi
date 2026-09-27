"""One customer must not be able to touch another customer's order.

`purchase_order` is keyed by the purchase order number alone, and real numbers
are unique only per buyer. So on 0.4.0 a partner could name a number it did
not own and have the mock act on it: an 860 cancelled a stranger's order, and
an ordinary 850 replaced one outright, taking the lines with it and leaving
the despatch and invoice already promised for it pointing at goods nobody
ordered.

The guard here is deliberately the *smallest* thing that makes it safe, with
no schema change, so it can go out as a patch. Keying orders by partner and
number is the real fix and is its own issue; until then a number somebody else
holds has to be refused, which is wrong in principle - two buyers may both own
a 4500000042 - and right for now, because the alternative is destroying the
first one.

Every test here asserts on the *victim's* order as much as on the answer: a
refusal that still corrupted the state would pass a test that only read the
receipt.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import documents

from support import (ACME, EURODIS, GLOBEX, INITECH, MockServerCase,
                     edifact_change, edifact_order, x12_change, x12_order)

EDIFACT = {"Content-Type": "application/edifact"}
# A window, so the order is still changeable when the second partner tries.
WINDOW = {"despatch_delay_ms": 3600 * 1000, "invoice_delay_ms": 3600 * 1000}

PO = "4500000042"


class WhoseOrderIsIt(unittest.TestCase):
    """The one place that knows a number is not an identity."""

    def test_the_partner_that_placed_it_owns_it(self):
        self.assertTrue(documents.belongs_to({"partner": "ACME"}, {"id": "ACME"}))

    def test_nobody_else_does(self):
        self.assertFalse(documents.belongs_to({"partner": "ACME"},
                                              {"id": "GLOBEX"}))

    def test_an_order_that_does_not_exist_belongs_to_nobody(self):
        self.assertFalse(documents.belongs_to(None, {"id": "ACME"}))

    def test_the_refusal_does_not_name_the_other_partner(self):
        # A customer has no business learning which numbers its competitors
        # use, so this is the same sentence an unknown order gets.
        self.assertEqual(documents.NOT_FOUND, "no such purchase order")
        self.assertNotIn("ACME", documents.NUMBER_IN_USE)


class AStrangersChange(MockServerCase):
    config_kwargs = WINDOW

    def setUp(self):
        super().setUp()
        self.send(x12_order(PO, sender=ACME))
        self.before = self.order(PO)

    def unchanged(self):
        after = self.order(PO)
        self.assertEqual(after["partner"], ACME)
        self.assertEqual(after["status"], self.before["status"])
        self.assertEqual([(row["line"], row["sku"], row["quantity"],
                           row["confirmed"], row["status"])
                          for row in after["lines"]],
                         [(row["line"], row["sku"], row["quantity"],
                           row["confirmed"], row["status"])
                          for row in self.before["lines"]])

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

    def test_a_restated_850_from_another_partner_is_refused(self):
        # BEG01 = 04 restates an order rather than placing one, and goes
        # through the same code a change does.
        summary = self.send(x12_order(PO, sender=INITECH, purpose="04",
                                      lines=(("WIDGET-001", 1, "12.50"),)))
        self.assertEqual(summary["orders"], [])
        self.assertEqual(summary["refusals"][0]["reason"], documents.NOT_FOUND)
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
        self.assertEqual(self.order(PO)["lines"][0]["confirmed"], "60")

    def test_the_owner_can_still_cancel_it(self):
        self.send(x12_change(PO, lines=[("1", "CA", 0, "0")], purpose="01",
                             sender=ACME))
        self.assertEqual(self.order(PO)["status"], "cancelled")


class AStrangersOrder(MockServerCase):
    """An ordinary 850 reusing a number somebody else holds."""

    config_kwargs = WINDOW

    def setUp(self):
        super().setUp()
        self.send(x12_order(PO, sender=ACME))

    def scheduled(self):
        _status, _headers, rows = self.get("/_mock/scheduled?all")
        return [(row["partner"], row["po_number"], row["kind"]) for row in rows]

    def steal(self):
        return self.send(x12_order(PO, sender=GLOBEX,
                                   lines=(("BRKT-050", 7, "4.15"),)))

    def test_it_is_refused(self):
        summary = self.steal()
        self.assertEqual(summary["orders"], [])
        self.assertEqual(summary["refusals"],
                         [{"order": PO, "reason": documents.NUMBER_IN_USE}])

    def test_the_first_order_survives_untouched(self):
        self.steal()
        order = self.order(PO)
        self.assertEqual(order["partner"], ACME)
        self.assertEqual([row["sku"] for row in order["lines"]],
                         ["WIDGET-001", "BRKT-050"])
        self.assertEqual(order["lines"][0]["quantity"], "100")

    def test_and_so_does_the_work_promised_for_it(self):
        before = self.scheduled()
        self.steal()
        self.assertEqual(self.scheduled(), before)
        self.assertTrue(all(partner == ACME for partner, _po, _kind in before))

    def test_the_855_rejects_every_line_and_says_why(self):
        self.steal()
        response = self.document(GLOBEX, "response")
        message = response.groups[0].messages[0]
        self.assertEqual(message.find("BAK").get(2), "RJ")
        acks = [item for item in message.segments if item.tag == "ACK"]
        self.assertTrue(acks)
        for item in acks:
            self.assertEqual(item.get(1), "IR")
            self.assertEqual(item.get(2), "0")
        reasons = [item.get(3) for item in message.segments
                   if item.tag == "REF" and item.get(1) == "ZZ"]
        self.assertIn(documents.NUMBER_IN_USE, reasons)

    def test_the_refusal_comes_after_the_acknowledgment(self):
        # The 997 says the syntax was read; the 855 says the order was not
        # taken. A refusal is not a reason to answer out of order.
        summary = self.steal()
        self.assertEqual([item["code"] for item in summary["queued"]],
                         ["997", "855"])

    def test_nothing_is_scheduled_for_the_refused_order(self):
        self.steal()
        self.assertEqual([row for row in self.scheduled()
                          if row[0] == GLOBEX], [])

    def test_the_same_partner_may_still_replace_its_own(self):
        summary = self.send(x12_order(PO, sender=ACME,
                                      lines=(("GEAR-100", 3, "8.90"),)))
        self.assertEqual(summary["orders"], [PO])
        self.assertEqual(summary["refusals"], [])
        order = self.order(PO)
        self.assertEqual(order["partner"], ACME)
        self.assertEqual([row["sku"] for row in order["lines"]], ["GEAR-100"])


class TheSameInEdifact(MockServerCase):
    config_kwargs = WINDOW

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
        order = self.order(PO)
        self.assertEqual(order["partner"], EURODIS)
        self.assertEqual(order["lines"][0]["confirmed"], "100")

    def test_an_orders_reusing_the_number_is_refused(self):
        summary = self.send(edifact_order(PO, sender=self.OTHER,
                                          lines=(("BRKT-050", 7, "4.15"),)),
                            headers=EDIFACT)
        self.assertEqual(summary["orders"], [])
        self.assertEqual(summary["refusals"][0]["reason"],
                         documents.NUMBER_IN_USE)
        self.assertEqual(self.order(PO)["partner"], EURODIS)

    def test_the_ordrsp_refusing_it_says_re(self):
        # EDIFACT's word for "rejected" in 4343, where X12 says RJ in BAK02.
        self.send(edifact_order(PO, sender=self.OTHER,
                                lines=(("BRKT-050", 7, "4.15"),)),
                  headers=EDIFACT)
        response = self.document(self.OTHER, "response")
        message = response.groups[0].messages[0]
        self.assertEqual(message.find("BGM").get(4), "RE")

    def test_the_owner_can_still_change_it(self):
        summary = self.send(edifact_change(PO, lines=[("1", "3", 60, "12.50")],
                                           sender=EURODIS), headers=EDIFACT)
        self.assertEqual(summary["changed"], [PO])


class TheRefusalIsAWellFormedDocument(MockServerCase):
    """It is built without a stored order, so it is worth checking twice."""

    config_kwargs = WINDOW

    def test_the_855_validates_against_the_mocks_own_dictionary(self):
        from mockedi import ack, validate
        self.send(x12_order(PO, sender=ACME))
        self.send(x12_order(PO, sender=GLOBEX,
                            lines=(("BRKT-050", 7, "4.15"),)))
        row = [item for item in self.mailbox(GLOBEX)
               if item["code"] == "855"][0]
        from support import parse
        report = validate.validate(parse(row["payload"]))
        self.assertTrue(report.clean, "\n".join(ack.explain(report)))

    def test_it_still_addresses_the_ship_to_the_buyer_named(self):
        # Nothing was stored, so the ship-to has to come off the order that
        # arrived; leaving it out writes an N1*ST with nothing in it.
        self.send(x12_order(PO, sender=ACME))
        self.send(x12_order(PO, sender=GLOBEX,
                            lines=(("BRKT-050", 7, "4.15"),)))
        message = self.document(GLOBEX, "response").groups[0].messages[0]
        ship_to = [item for item in message.segments
                   if item.tag == "N1" and item.get(1) == "ST"][0]
        self.assertTrue(ship_to.get(2))
        self.assertTrue(ship_to.get(4))


if __name__ == "__main__":
    unittest.main()
