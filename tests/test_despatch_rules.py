"""The buyer's 856 against the order and the 855: what was sent versus promised.

The five rules for an 856 or a DESADV (#151). Four are the issue's: a despatch
before anything was confirmed, a line the order does not have, more shipped
than confirmed, and more shipped than ordered. The fifth came out of reviewing
#153 - a consignment that names an item rather than a line number, where the
order has that item on two lines, which the matcher used to resolve by picking
the first.

Quantities are cumulative across consignments, because a split delivery is the
ordinary case and two consignments of 60 against an order of 100 is exactly
the failure the fourth rule exists for.

Like every disagreement, none of these reaches the 997: the last class here
is the guard.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import schema
from mockedi.claims import Claim, _lines_for_item

from test_reconciliation import AS_ORDERED, answer, line
from test_three_way_match import MatchCase, document, item
from support import parse

TWO_OF_A_KIND = [{"sku": "WIDGET-001", "quantity": "100", "uom": "EA",
                  "price": "12.50"},
                 {"sku": "WIDGET-001", "quantity": "40", "uom": "EA",
                  "price": "12.50"}]


def unnumbered(payload):
    """The same 856 with its line numbers removed, naming only the item.

    Both of them: the reader takes the line from LIN01 *or* SN101, so an 856
    that truly says only what was in the carton has to have neither.
    """
    out = payload
    for part in [p for p in payload.split("~")
                 if "LIN" in p or "SN1" in p]:
        fields = part.split("*")
        out = out.replace(part, "*".join([fields[0], ""] + fields[2:]))
    return out


class ADespatchThatAgrees(MatchCase):
    def test_says_nothing(self):
        self.confirmed()
        self.assertEqual(self.shipped()["disagreements"], [])

    def test_and_what_it_shipped_is_on_the_order(self):
        self.confirmed()
        self.shipped()
        self.assertEqual(self.order("PO-B")["reconciliation"][0]["shipped"], "100")

    def test_two_consignments_within_the_confirmation_say_nothing(self):
        self.confirmed()
        self.shipped([item("1", "WIDGET-001", 60)])
        self.assertEqual(self.shipped([item("1", "WIDGET-001", 40)])
                         ["disagreements"], [])


class TheRulesForADespatch(MatchCase):
    def test_a_despatch_before_any_855(self):
        summary = self.shipped()
        self.assertEqual([(d["rule"], d["expected"], d["found"])
                          for d in summary["disagreements"]],
                         [("shipped-before-confirmed", "an 855", "none")])

    def test_and_it_is_said_once_not_once_per_line(self):
        summary = self.shipped([item("1", "WIDGET-001", 100),
                                item("2", "BRKT-050", 40, price="4.15")])
        self.assertEqual([d["rule"] for d in summary["disagreements"]],
                         ["shipped-before-confirmed"])

    def test_a_line_the_order_does_not_have(self):
        self.confirmed()
        summary = self.shipped([item("9", "NOSUCH-1", 5)])
        self.assertEqual([(d["rule"], d["line"], d["found"])
                          for d in summary["disagreements"]],
                         [("shipped-unknown-line", "9", "5")])

    def test_more_shipped_than_confirmed(self):
        self.confirmed()
        summary = self.shipped([item("1", "WIDGET-001", 120)])
        self.assertIn(("shipped-more-than-confirmed", "1", "100", "120"),
                      self.rules(summary))

    def test_a_line_confirmed_at_nothing_and_shipped_anyway(self):
        self.confirmed([line("1", "WIDGET-001", 0, status="IR",
                             reason="out of stock", ordered=100),
                        AS_ORDERED[1]])
        summary = self.shipped([item("1", "WIDGET-001", 10)])
        found = [d for d in summary["disagreements"]
                 if d["rule"] == "shipped-more-than-confirmed"]
        self.assertEqual((found[0]["expected"], found[0]["found"]), ("0", "10"))
        self.assertIn("refused", found[0]["note"])

    def test_two_consignments_together_exceed_the_confirmation(self):
        # Neither is too much on its own; together they are.
        self.confirmed()
        self.shipped([item("1", "WIDGET-001", 60)])
        summary = self.shipped([item("1", "WIDGET-001", 60)])
        self.assertIn(("shipped-more-than-confirmed", "1", "100", "120"),
                      self.rules(summary))

    def test_more_shipped_than_ordered_with_no_855_at_all(self):
        # The only one of the two quantity rules that can speak here.
        summary = self.shipped([item("1", "WIDGET-001", 130)])
        self.assertIn(("shipped-more-than-ordered", "1", "100", "130"),
                      self.rules(summary))
        self.assertNotIn("shipped-more-than-confirmed",
                         [d["rule"] for d in summary["disagreements"]])

    def test_overshipping_a_matched_confirmation_says_both(self):
        # The decision recorded on #151: these are two different sentences to
        # a buyer, and both are true, so both are said.
        self.confirmed()
        summary = self.shipped([item("1", "WIDGET-001", 130)])
        self.assertEqual({d["rule"] for d in summary["disagreements"]},
                         {"shipped-more-than-confirmed", "shipped-more-than-ordered"})


class AConsignmentThatNamesOnlyAnItem(MatchCase):
    """The matcher fix from #153's review, and the rule that replaces guessing."""

    def order_of_two_alike(self):
        status, _h, data = self.post("/_mock/purchase", {
            "partner": self.partner, "po_number": "PO-SAME",
            "lines": TWO_OF_A_KIND})
        self.assertEqual(status, 201, data)

    def test_one_line_ordered_it_so_it_finds_its_line(self):
        self.confirmed()
        summary = self.deliver(unnumbered(document(
            schema.DESPATCH, "PO-B", [item("1", "WIDGET-001", 100)],
            sender=self.partner, dialect=self.dialect)))
        self.assertEqual(summary["disagreements"], [])
        self.assertEqual(self.order("PO-B")["reconciliation"][0]["shipped"], "100")

    def test_two_lines_ordered_it_so_the_mock_refuses_to_choose(self):
        self.order_of_two_alike()
        summary = self.deliver(unnumbered(document(
            schema.DESPATCH, "PO-SAME", [item("1", "WIDGET-001", 100)],
            sender=self.partner, dialect=self.dialect)))
        found = [d for d in summary["disagreements"]
                 if d["rule"] == "shipped-ambiguous-item"]
        self.assertEqual(len(found), 1, summary["disagreements"])
        self.assertEqual(found[0]["expected"], "1, 2")

    def test_and_it_is_counted_against_neither_line(self):
        self.order_of_two_alike()
        self.deliver(unnumbered(document(
            schema.DESPATCH, "PO-SAME", [item("1", "WIDGET-001", 100)],
            sender=self.partner, dialect=self.dialect)))
        self.assertEqual([row["shipped"] for row in
                          self.order("PO-SAME")["reconciliation"]], ["0", "0"])

    def test_an_exact_sku_beats_a_upc_that_collides_elsewhere(self):
        # The `or` this replaces let whichever line came first win, so a claim
        # naming line 2's item could be put on line 1.
        lines = {"1": {"sku": "A", "upc": "U-2"}, "2": {"sku": "B", "upc": "U-2"}}
        self.assertEqual(_lines_for_item(lines, Claim(line="", sku="B",
                                                      upc="U-2")), ["2"])

    def test_and_a_upc_is_still_the_fallback_when_no_sku_matches(self):
        lines = {"1": {"sku": "A", "upc": "U-1"}, "2": {"sku": "B", "upc": "U-2"}}
        self.assertEqual(_lines_for_item(lines, Claim(line="", sku="Z",
                                                      upc="U-2")), ["2"])


class InEdifact(TheRulesForADespatch):
    dialect = "EDIFACT"


class TheAcknowledgmentIsUnchanged(MatchCase):
    """The rule that decides the design: a disagreement never reaches a 997."""

    def verdict(self, summary):
        row = self.mailbox(self.partner, "acknowledgment")[-1]
        message = parse(row["payload"]).groups[0].messages[0]
        return ([seg.get(1) for seg in message.find_all("AK5")],
                message.find("AK9").get(1),
                summary["transactionSets"][0]["accepted"],
                summary["transactionSets"][0]["findings"])

    def test_a_disagreeing_856_is_acknowledged_as_a_clean_one_is(self):
        self.confirmed()
        clean = self.verdict(self.shipped())
        disagreeing = self.shipped([item("1", "WIDGET-001", 130),
                                    item("9", "NOSUCH-1", 5)])
        self.assertTrue(len(disagreeing["disagreements"]) >= 3)
        self.assertEqual(self.verdict(disagreeing), clean)
        self.assertEqual(clean, (["A"], "A", True, []))


if __name__ == "__main__":
    unittest.main()
