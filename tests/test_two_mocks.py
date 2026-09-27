"""Two mocks, wired to each other: one selling, one buying.

The test this project is uniquely placed to write. One mock is the seller it
has always been; the other places the order and receives what comes back. Every
seller behaviour is then a supplier misbehaving on demand, over real AS2, and
the buyer's side of the conversation is what we assert on.

It tests both directions at once. A seller behaviour that produced the wrong
document would fail here as surely as a buyer that filed it under the wrong
order - which is the point of running the two against each other instead of
against fixtures.

What is *not* here yet: the disagreements. Comparing what the supplier claimed
against what was ordered is the rest of #126, and these tests assert on what
the buyer received and filed. The assertions on claimed quantities arrive with
the rules.

One guard is here from the start, because it is the rule the whole design rests
on: a flow full of disagreements must produce the same 997s as a clean flow.
A disagreement is not a syntax error, and the moment one reaches a 997 a
supplier's integration learns that a correct document can be bounced at the
syntax layer.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi.testing import Document, Mock

# Every test uses purchase order numbers of its own. Orders are keyed by number
# alone today and by partner and number shortly; distinct numbers mean nothing
# here has to change when they are.
LINES = [{"sku": "WIDGET-001", "quantity": "100", "uom": "EA",
          "price": "12.50"},
         {"sku": "BRKT-050", "quantity": "40", "uom": "EA", "price": "4.15"}]


class Pair:
    """A seller and a buyer, each the other's trading partner.

    Pushing the conversation through is `Mock.exchange`, which this file's own
    copy of became: a rally between two mocks, with each clock moved so that
    work only *promised* - the second invoice `duplicate-invoice` sends - is
    not mistaken for a document that never arrived. #141 has the story.
    """

    def __init__(self, dialect="X12", behaviour="accept"):
        self.dialect = dialect
        self.seller = Mock.start(as2_id="SELLCO")
        self.buyer = Mock.start(as2_id="BUYCO")
        self.seller.expect(
            "POST", "/_mock/partners",
            {"id": "BUYCO", "name": "Buy Co", "dialect": dialect,
             "behaviour": behaviour, "as2_url": self.buyer.base + "/edi"},
            status=201)
        self.buyer.expect(
            "POST", "/_mock/partners",
            {"id": "SELLCO", "name": "Sell Co", "dialect": dialect,
             "role": "supplier", "as2_url": self.seller.base + "/edi"},
            status=201)

    def close(self):
        self.seller.close()
        self.buyer.close()

    def place(self, po_number, lines=None):
        return self.buyer.expect(
            "POST", "/_mock/purchase",
            {"partner": "SELLCO", "po_number": po_number,
             "lines": lines if lines is not None else LINES}, status=201)

    def exchange(self):
        self.buyer.exchange(self.seller)

    # -- what the buyer made of it

    def received(self):
        """The codes the buyer received from the supplier, newest last."""
        rows = self.buyer.documents(direction="in", limit=50)
        return [row["code"] for row in reversed(rows)
                if row["kind"] != "acknowledgment"]

    def filed(self, po_number):
        rows = self.buyer.documents(direction="in", limit=50)
        return [row["code"] for row in reversed(rows)
                if row["reference"] == po_number]

    def inbound(self, code):
        """Every document of one code the buyer received, parsed."""
        out = []
        for row in reversed(self.buyer.documents(direction="in", code=code,
                                                 limit=20)):
            full = self.buyer.expect("GET", "/_mock/documents/%d" % row["id"])
            out.append(Document(full["payload"]))
        return out

    def verdicts(self):
        """What each receipt the buyer sent says, by the document it answers.

        Not the receipts themselves: control numbers are allocated per
        interchange, so two runs of the same flow never produce the same ones,
        and a flow whose documents arrive in a different order gets them in a
        different order too. What has to match is the *verdict* on each kind of
        document, which is the thing a disagreement must not change.

        A 997 is `{"855": ["A", "A"]}` - AK5 then AK9. A CONTRL is
        `{"ORDRSP": ["7", "7"]}` - UCI's action then UCM's. Any error detail
        they carried would land in the same list, which is the point.
        """
        out = {}
        for row in reversed(self.buyer.documents(direction="out", limit=50)):
            if row["code"] not in ("997", "CONTRL"):
                continue
            full = self.buyer.expect("GET", "/_mock/documents/%d" % row["id"])
            document = Document(full["payload"])
            if row["code"] == "997":
                answered = document.find("AK2")
                code = answered.get(1) if answered is not None else "?"
                said = [item.get(1) for item in document.all("AK5")]
                said += [item.get(1) for item in document.all("AK9")]
                said += ["AK3:%s" % item.get(4) for item in document.all("AK3")]
                said += ["AK4:%s" % item.get(3) for item in document.all("AK4")]
            else:
                answered = document.find("UCM")
                code = answered.comp(2, 1) if answered is not None else "?"
                said = [item.get(4) for item in document.all("UCI")]
                said += [item.get(3) for item in document.all("UCM")]
                said += ["UCS:%s" % item.get(2) for item in document.all("UCS")]
            out.setdefault(code, []).append(said)
        return out


class BothDialects(unittest.TestCase):
    """Subclasses run every test once per dialect."""

    behaviour = "accept"
    dialects = ("X12", "EDIFACT")

    def pair(self, dialect, behaviour=None):
        pair = Pair(dialect, behaviour or self.behaviour)
        self.addCleanup(pair.close)
        return pair


class AWholeOrderToCashRunsBetweenThem(BothDialects):

    def test_the_buyer_receives_the_three_answers_in_order(self):
        expected = {"X12": ["855", "856", "810"],
                    "EDIFACT": ["ORDRSP", "DESADV", "INVOIC"]}
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-CLEAN-" + dialect[:3])
                pair.exchange()
                self.assertEqual(pair.received(), expected[dialect])

    def test_each_one_is_filed_against_the_order(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                po_number = "PO-FILED-" + dialect[:3]
                pair.place(po_number)
                pair.exchange()
                self.assertEqual(len(pair.filed(po_number)), 3)

    def test_the_order_the_supplier_received_is_the_one_that_was_placed(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                po_number = "PO-SAME-" + dialect[:3]
                pair.place(po_number)
                pair.exchange()
                theirs = pair.seller.order(po_number)
                self.assertEqual(theirs["partner"], "BUYCO")
                self.assertEqual([row["sku"] for row in theirs["lines"]],
                                 ["WIDGET-001", "BRKT-050"])
                self.assertEqual(theirs["lines"][0]["quantity"], "100")

    def test_the_buyers_timeline_tells_the_whole_story(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                po_number = "PO-STORY-" + dialect[:3]
                pair.place(po_number)
                pair.exchange()
                events = pair.buyer.timeline(po_number)["events"]
                kinds = [event["event"] for event in events]
                self.assertEqual(kinds[0], "ordered")
                self.assertEqual(kinds[1], "sent")
                self.assertEqual(kinds.count("received"), 3)
                self.assertIn("acknowledged", kinds)

    def test_the_supplier_acknowledged_the_order(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                po_number = "PO-ACKED-" + dialect[:3]
                pair.place(po_number)
                pair.exchange()
                acked = [event for event in
                         pair.buyer.timeline(po_number)["events"]
                         if event["event"] == "acknowledged"]
                self.assertTrue(acked)
                self.assertEqual(acked[0]["status"], "accepted")

    def test_nothing_is_left_undelivered_on_either_side(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-DONE-" + dialect[:3])
                pair.exchange()
                for name, mock in (("buyer", pair.buyer),
                                   ("seller", pair.seller)):
                    stuck = [row for row in mock.outbox()
                             if row["status"] not in ("delivered", "collected")]
                    self.assertEqual(stuck, [], name)


class WhatTheSupplierConfirmed(BothDialects):
    """`short-ship`: the supplier commits to less than was asked for."""

    behaviour = "short-ship"

    def response(self, pair):
        code = "855" if pair.dialect == "X12" else "ORDRSP"
        return pair.inbound(code)[0].as_response()

    def test_a_line_is_confirmed_short(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-SHORT-" + dialect[:3])
                pair.exchange()
                response = self.response(pair)
                self.assertTrue(response.short,
                               "the supplier confirmed everything in full")
                line = response.short[0]
                self.assertGreater(line.quantity, line.confirmed)

    def test_what_ships_and_what_is_billed_match_the_confirmation(self):
        # The seller is consistent with itself, so a three-way match over
        # these documents has nothing to report. That is the case that must
        # *not* produce a finding when the rules land.
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-SHIPQ-" + dialect[:3])
                pair.exchange()
                response = self.response(pair)
                confirmed = {line.number: line.confirmed
                             for line in response.lines}
                despatch_code = "856" if dialect == "X12" else "DESADV"
                invoice_code = "810" if dialect == "X12" else "INVOIC"
                despatch = pair.inbound(despatch_code)[0].as_despatch()
                invoice = pair.inbound(invoice_code)[0].as_invoice()
                for item in despatch.items:
                    self.assertEqual(item.quantity, confirmed[item.line])
                for line in invoice.lines:
                    self.assertEqual(line.quantity, confirmed[line.number])


class WhatTheSupplierRefused(BothDialects):
    """`reject-line`: one line is refused outright."""

    behaviour = "reject-line"

    def test_a_line_comes_back_rejected(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-REJ-" + dialect[:3])
                pair.exchange()
                code = "855" if dialect == "X12" else "ORDRSP"
                response = pair.inbound(code)[0].as_response()
                self.assertTrue(response.rejected)
                self.assertEqual(response.rejected[0].confirmed, 0)

    def test_nothing_is_shipped_or_billed_for_it(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-REJQ-" + dialect[:3])
                pair.exchange()
                code = "855" if dialect == "X12" else "ORDRSP"
                refused = {line.number for line
                           in pair.inbound(code)[0].as_response().rejected}
                self.assertTrue(refused)
                despatch_code = "856" if dialect == "X12" else "DESADV"
                invoice_code = "810" if dialect == "X12" else "INVOIC"
                shipped = {item.line for item
                           in pair.inbound(despatch_code)[0].as_despatch().items}
                billed = {line.number for line
                          in pair.inbound(invoice_code)[0].as_invoice().lines}
                self.assertEqual(refused & shipped, set())
                self.assertEqual(refused & billed, set())


class AnInvoiceNumberArrivesTwice(BothDialects):
    """`duplicate-invoice`: the supplier has a retry bug."""

    behaviour = "duplicate-invoice"

    def test_two_invoices_arrive(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-DUP-" + dialect[:3])
                pair.exchange()
                code = "810" if dialect == "X12" else "INVOIC"
                self.assertEqual(len(pair.inbound(code)), 2)

    def test_and_they_carry_the_same_invoice_number(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-DUPN-" + dialect[:3])
                pair.exchange()
                code = "810" if dialect == "X12" else "INVOIC"
                numbers = [document.as_invoice().invoice_number
                           for document in pair.inbound(code)]
                self.assertEqual(len(set(numbers)), 1, numbers)

    def test_the_second_is_accepted_at_the_syntax_layer(self):
        # It is a well-formed invoice. That it should not have been sent is a
        # business matter, and a 997 is not where that is said.
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-DUPA-" + dialect[:3])
                pair.exchange()
                code = "810" if dialect == "X12" else "INVOIC"
                rows = pair.buyer.documents(direction="in", code=code, limit=5)
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertTrue(row["accepted"], row)
                    self.assertEqual(row["findings"], [])


class TheAnswersArriveInTheWrongOrder(BothDialects):
    """`out-of-order`: the invoice before the despatch, the response last."""

    behaviour = "out-of-order"

    def test_the_invoice_arrives_before_the_despatch(self):
        expected = {"X12": ["810", "856", "855"],
                    "EDIFACT": ["INVOIC", "DESADV", "ORDRSP"]}
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-OOO-" + dialect[:3])
                pair.exchange()
                self.assertEqual(pair.received(), expected[dialect])

    def test_all_three_are_still_filed_against_the_order(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                po_number = "PO-OOOF-" + dialect[:3]
                pair.place(po_number)
                pair.exchange()
                self.assertEqual(len(pair.filed(po_number)), 3)


class TheSupplierNeverBills(BothDialects):
    """`no-invoice`: it ships and the invoice never comes."""

    behaviour = "no-invoice"

    def test_the_despatch_arrives_and_the_invoice_does_not(self):
        expected = {"X12": ["855", "856"], "EDIFACT": ["ORDRSP", "DESADV"]}
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                pair.place("PO-NOINV-" + dialect[:3])
                pair.exchange()
                self.assertEqual(pair.received(), expected[dialect])

    def test_the_buyers_timeline_shows_the_gap(self):
        for dialect in self.dialects:
            with self.subTest(dialect=dialect):
                pair = self.pair(dialect)
                po_number = "PO-NOINVT-" + dialect[:3]
                pair.place(po_number)
                pair.exchange()
                codes = [event.get("code") for event
                         in pair.buyer.timeline(po_number)["events"]
                         if event["event"] == "received"]
                self.assertNotIn("810", codes)
                self.assertNotIn("INVOIC", codes)


class ADisagreementIsNotASyntaxError(unittest.TestCase):
    """The rule the whole of #126 rests on, guarded before the rules exist.

    A buyer that receives a well-formed 856 shipping more than was confirmed
    sends `AK5*A` and takes the dispute elsewhere. If the mock answered such a
    document with a rejection, a supplier's integration would learn that a
    correct ASN can be bounced at the syntax layer - which is false, and
    expensive to unlearn.

    So the receipts a misbehaving flow produces must be the same as a clean
    flow's. This compares the segments that carry the verdict, since control
    numbers and dates differ between runs by design.
    """

    ACCEPTED = {"X12": {"A"}, "EDIFACT": {"7"}}

    def verdicts_for(self, dialect, behaviour, po_number):
        pair = Pair(dialect, behaviour)
        self.addCleanup(pair.close)
        pair.place(po_number)
        pair.exchange()
        return pair.verdicts()

    def test_a_short_shipped_flow_is_acknowledged_as_a_clean_one_is(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                clean = self.verdicts_for(dialect, "accept",
                                          "PO-G1-" + dialect[:3])
                short = self.verdicts_for(dialect, "short-ship",
                                          "PO-G2-" + dialect[:3])
                self.assertEqual(short, clean)

    def test_so_is_a_flow_with_a_rejected_line(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                clean = self.verdicts_for(dialect, "accept",
                                          "PO-G3-" + dialect[:3])
                refused = self.verdicts_for(dialect, "reject-line",
                                            "PO-G4-" + dialect[:3])
                self.assertEqual(refused, clean)

    def test_and_so_is_one_that_arrives_in_the_wrong_order(self):
        # The same verdict on each kind of document. Only the order the
        # documents arrived in changed, and a receipt does not comment on that.
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                clean = self.verdicts_for(dialect, "accept",
                                          "PO-G5-" + dialect[:3])
                jumbled = self.verdicts_for(dialect, "out-of-order",
                                            "PO-G6-" + dialect[:3])
                self.assertEqual(jumbled, clean)

    def test_a_repeated_invoice_is_acknowledged_twice_and_accepted_twice(self):
        for dialect, code in (("X12", "810"), ("EDIFACT", "INVOIC")):
            with self.subTest(dialect=dialect):
                verdicts = self.verdicts_for(dialect, "duplicate-invoice",
                                             "PO-G8-" + dialect[:3])
                self.assertEqual(len(verdicts[code]), 2, verdicts)
                self.assertEqual(verdicts[code][0], verdicts[code][1])

    def test_every_receipt_accepts_everything_it_answers(self):
        for dialect in ("X12", "EDIFACT"):
            for behaviour in ("short-ship", "reject-line",
                              "duplicate-invoice", "out-of-order"):
                with self.subTest(dialect=dialect, behaviour=behaviour):
                    verdicts = self.verdicts_for(
                        dialect, behaviour,
                        "PO-G7-%s-%s" % (dialect[:3], behaviour[:5]))
                    self.assertTrue(verdicts)
                    for code, receipts in verdicts.items():
                        for said in receipts:
                            self.assertTrue(said, code)
                            self.assertEqual(set(said) - self.ACCEPTED[dialect],
                                             set(),
                                             "%s was not accepted cleanly: %s"
                                             % (code, said))


if __name__ == "__main__":
    unittest.main()
