"""The mock as buyer, comparing what a supplier says against what it ordered.

A disagreement is a business finding, not a syntax finding: it is reported in
the receipt, the order view, the timeline and `/_mock/disagreements`, and it
never enters a 997 or a CONTRL. These are the five rules for an 855 or an
ORDRSP (#126), the places they surface, and the guard that a flow with
disagreements is acknowledged exactly as a clean one. The 856's rules are
#151 and the 810's #152.
"""
import datetime
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, edifact, schema, transactions, validate, x12
from mockedi.transactions import Party
from mockedi.validate import BusinessFinding, MessageReport

from support import MockServerCase, _next_control, parse
from test_two_mocks import Pair

NORTHWIND = "NORTHWIND"
NORDIC = "NORDIC"
EDIFACT = {"Content-Type": "application/edifact"}
WHEN = datetime.datetime(2026, 9, 28, 10, 0)
ORDERED = [{"sku": "WIDGET-001", "quantity": "100", "uom": "EA", "price": "12.50"},
           {"sku": "BRKT-050", "quantity": "40", "uom": "EA", "price": "4.15"}]


def line(number, sku, confirmed, status="IA", price="12.50", ordered=None,
         reason="", upc=""):
    """One line of a supplier's answer, as the seller-side writers take it."""
    return {"line": number, "sku": sku, "upc": upc, "description": "",
            "quantity": str(ordered if ordered is not None else confirmed),
            "uom": "EA", "price": price, "ordered_price": price, "status": status,
            "confirmed": str(confirmed), "shipped": "0", "invoiced": "0",
            "reason": reason, "scheduled_on": ""}


AS_ORDERED = [line("1", "WIDGET-001", 100), line("2", "BRKT-050", 40, price="4.15")]


def answer(po_number, lines, sender=NORTHWIND, dialect="X12", kind=schema.RESPONSE):
    """A supplier's 855 or ORDRSP, written by the mock's own seller-side writer."""
    supplier = Party(role="SE", name="Supplier", identifier=sender, country="US")
    mock = {"id": "MOCKEDI", "name": "Mock EDI", "qualifier": "ZZ", "street": "",
            "city": "", "region": "", "postal": "", "country": "US", "duns": "",
            "dialect": dialect, "version": "004010"}
    order = {"po_number": po_number, "seller_order": "SO-1", "ordered_on":
             "2026-09-27", "requested_on": "", "currency": "USD",
             "ship_to_name": "Mock EDI", "ship_to_id": "MOCKEDI"}
    body = transactions.write_response(dialect, supplier, mock, order, lines, WHEN)
    code = schema.set_code(dialect, kind)
    control = _next_control(9)
    if dialect == "X12":
        return x12.render(x12.wrap([x12.message(code, "0001", body)], sender,
                                   "MOCKEDI", control, control.lstrip("0"),
                                   schema.lookup(dialect, code).group))
    return edifact.render(edifact.wrap([edifact.message(code, "1", body, "D:96A:UN")],
                                       sender, "MOCKEDI", control))


class ReconcilingCase(MockServerCase):
    def setUp(self):
        super().setUp()
        self.placed("PO-R")

    def placed(self, po_number, partner=NORTHWIND):
        status, _h, data = self.post("/_mock/purchase", {
            "partner": partner, "po_number": po_number, "lines": ORDERED})
        self.assertEqual(status, 201, data)
        return data

    def says(self, lines, po_number="PO-R", **kw):
        headers = EDIFACT if kw.get("dialect") == "EDIFACT" else None
        summary = self.send(answer(po_number, lines, **kw), headers=headers)
        self.assertTrue(summary["accepted"], summary["transactionSets"])
        return summary

    def rules(self, summary):
        return [(d["rule"], d["line"], d["expected"], d["found"])
                for d in summary["disagreements"]]


class AnAnswerThatAgrees(ReconcilingCase):
    def test_says_nothing(self):
        summary = self.says(AS_ORDERED)
        self.assertEqual(summary["disagreements"], [])
        self.assertEqual(summary["transactionSets"][0]["disagreements"], [])

    def test_and_the_order_shows_what_was_confirmed(self):
        self.says(AS_ORDERED)
        order = self.order("PO-R")
        self.assertEqual([(r["line"], r["ordered"], r["confirmed"], r["status"])
                          for r in order["reconciliation"]],
                         [("1", "100", "100", "IA"), ("2", "40", "40", "IA")])
        self.assertEqual(order["disagreements"], [])


class TheRulesForAnAnswer(ReconcilingCase):
    """One comparison each, naming both numbers."""

    def test_a_line_the_order_does_not_have(self):
        summary = self.says(AS_ORDERED + [line("3", "GEAR-100", 5, price="8.90")])
        self.assertEqual(self.rules(summary),
                         [("confirmed-unknown-line", "3", "", "5")])
        self.assertIn("line 3", summary["disagreements"][0]["note"])

    def test_more_than_was_ordered(self):
        summary = self.says([line("1", "WIDGET-001", 120), AS_ORDERED[1]])
        self.assertEqual(self.rules(summary), [("confirmed-more", "1", "100", "120")])
        self.assertEqual(summary["disagreements"][0]["note"],
                         "line 1: ordered 100, 855 confirms 120")

    def test_less_than_ordered_with_a_reason(self):
        summary = self.says([line("1", "WIDGET-001", 80, status="IQ", ordered=100,
                                  reason="Only 80 in stock"), AS_ORDERED[1]])
        self.assertEqual(self.rules(summary), [("confirmed-less", "1", "100", "80")])
        self.assertIn("IQ: Only 80 in stock", summary["disagreements"][0]["note"])

    def test_less_than_ordered_claiming_full_acceptance(self):
        summary = self.says([line("1", "WIDGET-001", 80, status="IA", ordered=100),
                             AS_ORDERED[1]])
        self.assertIn("says IA, accepted as ordered",
                      summary["disagreements"][0]["note"])

    def test_less_than_ordered_with_no_reason(self):
        summary = self.says([line("1", "WIDGET-001", 80, status="IQ", ordered=100),
                             AS_ORDERED[1]])
        self.assertIn("IQ, and gives no reason", summary["disagreements"][0]["note"])

    def test_a_refused_line_is_confirmed_at_nothing(self):
        summary = self.says([AS_ORDERED[0],
                             line("2", "BRKT-050", 0, status="IR", price="4.15",
                                  ordered=40, reason="Discontinued")])
        self.assertEqual(self.rules(summary), [("confirmed-less", "2", "40", "0")])

    def test_a_price_that_differs(self):
        summary = self.says([line("1", "WIDGET-001", 100, price="13.10"),
                             AS_ORDERED[1]])
        self.assertEqual(self.rules(summary),
                         [("price-differs", "1", "12.50", "13.10")])

    def test_a_price_change_admitted_with_ip_is_still_reported(self):
        summary = self.says([line("1", "WIDGET-001", 100, status="IP",
                                  price="13.10"), AS_ORDERED[1]])
        self.assertEqual(self.rules(summary)[0][0], "price-differs")
        self.assertIn("IP, price changed", summary["disagreements"][0]["note"])

    def test_an_item_substituted_without_saying_so(self):
        summary = self.says([line("1", "WIDGET-002", 100), AS_ORDERED[1]])
        self.assertEqual(self.rules(summary),
                         [("substituted", "1", "WIDGET-001", "WIDGET-002")])

    def test_but_not_when_it_says_is(self):
        summary = self.says([line("1", "WIDGET-002", 100, status="IS"),
                             AS_ORDERED[1]])
        self.assertEqual(summary["disagreements"], [])

    def test_in_edifact_too(self):
        self.post("/_mock/partners", {"id": NORDIC, "dialect": "EDIFACT",
                                      "version": "D:96A:UN", "role": "supplier",
                                      "qualifier": "14"})
        self.placed("PO-EU", partner=NORDIC)
        summary = self.says([line("1", "WIDGET-001", 120, price="13.10"),
                             AS_ORDERED[1]], po_number="PO-EU", sender=NORDIC,
                            dialect="EDIFACT")
        self.assertEqual(sorted(rule for rule, *_ in self.rules(summary)),
                         ["confirmed-more", "price-differs"])
        self.assertEqual(summary["disagreements"][0]["code"], "ORDRSP")


class WhatTheOrderHolds(ReconcilingCase):
    def test_a_corrected_answer_replaces_the_first(self):
        self.says([line("1", "WIDGET-001", 80, status="IQ", ordered=100,
                        reason="Short"), AS_ORDERED[1]])
        self.says(AS_ORDERED)
        order = self.order("PO-R")
        self.assertEqual(order["reconciliation"][0]["confirmed"], "100")
        # Both findings stay on record: each names the document it came from.
        self.assertEqual([d["rule"] for d in order["disagreements"]],
                         ["confirmed-less"])

    def test_each_document_is_named(self):
        summary = self.says([line("1", "WIDGET-001", 120), AS_ORDERED[1]])
        found = summary["disagreements"][0]
        control = summary["transactionSets"][0]["control"]
        self.assertEqual((found["code"], found["control"], found["order"]),
                         ("855", control, "PO-R"))


class WhereTheyShow(ReconcilingCase):
    def setUp(self):
        super().setUp()
        self.summary = self.says([line("1", "WIDGET-001", 120), AS_ORDERED[1]])

    def test_the_order_view(self):
        order = self.order("PO-R")
        self.assertEqual([(d["rule"], d["expected"], d["found"])
                          for d in order["disagreements"]],
                         [("confirmed-more", "100", "120")])

    def test_the_timeline_on_the_event_that_said_it(self):
        _s, _h, data = self.get("/_mock/orders/PO-R/timeline")
        received = [e for e in data["events"] if e["event"] == "received"
                    and e["code"] == "855"][0]
        self.assertEqual([d["rule"] for d in received["disagreements"]],
                         ["confirmed-more"])
        self.assertIn("disagrees with the order: line 1: ordered 100, 855 "
                      "confirms 120", received["summary"])
        self.assertTrue(received["accepted"])

    def test_their_own_endpoint(self):
        self.placed("PO-OTHER")
        self.says(AS_ORDERED, po_number="PO-OTHER")
        _s, _h, rows = self.get("/_mock/disagreements?partner=NORTHWIND&po=PO-R")
        self.assertEqual([row["rule"] for row in rows], ["confirmed-more"])
        _s, _h, rows = self.get("/_mock/disagreements?po=PO-OTHER")
        self.assertEqual(rows, [])

    def test_explain_says_them_as_business_not_syntax(self):
        report = MessageReport(code="855", control="0001")
        report.disagreements.append(BusinessFinding(
            rule="confirmed-more", kind="response", code="855", control="0001",
            po_number="PO-R", line="1", expected="100", found="120",
            note="line 1: ordered 100, 855 confirms 120"))
        self.assertTrue(report.clean)
        interchange = validate.InterchangeReport(dialect="X12", control="1",
                                                 messages=[report])
        self.assertIn("  disagrees with the order at line 1: line 1: ordered 100, "
                      "855 confirms 120 (confirmed-more)", ack.explain(interchange))

    def test_a_reset_clears_them(self):
        self.post("/_mock/reset")
        _s, _h, rows = self.get("/_mock/disagreements")
        self.assertEqual(rows, [])


class TheAcknowledgmentIsUnchanged(ReconcilingCase):
    """The rule that decides the design: a disagreement never reaches a 997."""

    def verdict(self, summary, partner=NORTHWIND):
        ack_row = self.mailbox(partner, "acknowledgment")[-1]
        message = parse(ack_row["payload"]).groups[0].messages[0]
        return ([s.get(1) for s in message.find_all("AK5")],
                message.find("AK9").get(1), summary["transactionSets"][0]["accepted"],
                summary["transactionSets"][0]["findings"])

    def test_a_disagreeing_855_is_acknowledged_as_a_clean_one_is(self):
        clean = self.verdict(self.says(AS_ORDERED))
        self.placed("PO-R2")
        disagreeing = self.says([line("1", "WIDGET-002", 120, price="13.10"),
                                 line("2", "BRKT-050", 0, status="IR", price="4.15",
                                      ordered=40),
                                 line("3", "GEAR-100", 5, price="8.90")],
                                po_number="PO-R2")
        self.assertEqual(len(disagreeing["disagreements"]), 5)
        self.assertEqual(self.verdict(disagreeing), clean)
        self.assertEqual(clean, (["A"], "A", True, []))


class TwoMocks(unittest.TestCase):
    """A real seller's behaviours, as the buyer reads them (#135's pair)."""

    def run_flow(self, behaviour, dialect="X12"):
        pair = Pair(dialect=dialect, behaviour=behaviour)
        self.addCleanup(pair.close)
        pair.place("PO-2M")
        pair.exchange()
        order = pair.buyer.order("PO-2M")
        return pair, order

    def test_accept_disagrees_with_nothing(self):
        for dialect in ("X12", "EDIFACT"):
            _pair, order = self.run_flow("accept", dialect)
            self.assertEqual(order["disagreements"], [], dialect)
            self.assertEqual([(r["confirmed"], r["shipped"], r["billed"])
                              for r in order["reconciliation"]],
                             [("100", "100", "100"), ("40", "40", "40")], dialect)

    def test_short_ship_is_confirmed_less_than_ordered(self):
        for dialect in ("X12", "EDIFACT"):
            _pair, order = self.run_flow("short-ship", dialect)
            self.assertEqual({d["rule"] for d in order["disagreements"]},
                             {"confirmed-less"}, dialect)
            self.assertEqual([(d["line"], d["expected"], d["found"])
                              for d in order["disagreements"]],
                             [("1", "100", "80"), ("2", "40", "32")], dialect)

    def test_reject_line_is_a_line_confirmed_at_nothing(self):
        _pair, order = self.run_flow("reject-line")
        self.assertEqual([(d["rule"], d["line"], d["found"])
                          for d in order["disagreements"]],
                         [("confirmed-less", "2", "0")])
        self.assertEqual(order["reconciliation"][1]["shipped"], "0")

    def test_and_the_buyer_acknowledges_both_flows_alike(self):
        clean, _order = self.run_flow("accept")
        short, _order = self.run_flow("short-ship")
        self.assertEqual(short.verdicts(), clean.verdicts())


if __name__ == "__main__":
    unittest.main(verbosity=2)
