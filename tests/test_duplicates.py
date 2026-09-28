"""A replayed interchange is refused, not fulfilled twice.

A retry bug on the sender's side is ordinary, and processing a duplicate
order is expensive - two shipments, two invoices, and a conversation with
somebody's accounts department. A real receiver detects the repeated control
number and refuses the copy in the envelope's own words.

The mock could already *produce* that bug, with the `duplicate-invoice`
behaviour. This is the other side of it: the thing a buyer's retry logic has
to be tested against.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import (ACME, EURODIS, MockServerCase, edifact_order, parse,
                     x12_order)

EDIFACT = {"Content-Type": "application/edifact"}


class ReplayingAnX12Interchange(MockServerCase):
    def setUp(self):
        super().setUp()
        self.payload = x12_order("PO-REPLAY", control="000000077", group="77")
        self.first = self.send(self.payload)

    def ta1(self):
        """The TA1's elements. It rides in the envelope, ahead of any group."""
        rows = self.mailbox(ACME, "interchange-acknowledgment")
        self.assertEqual(len(rows), 1, "expected exactly one TA1")
        for line in rows[0]["payload"].replace("\n", "").split("~"):
            if line.startswith("TA1*"):
                return line.split("*")
        self.fail("no TA1 segment in %r" % rows[0]["payload"])

    def test_the_first_one_is_fulfilled(self):
        self.assertTrue(self.first["accepted"])
        self.assertEqual([q["code"] for q in self.first["queued"]],
                         ["997", "855", "856", "810"])

    def test_the_second_is_refused(self):
        again = self.send(self.payload)
        self.assertFalse(again["accepted"])
        self.assertEqual(again["orders"], [])

    def test_and_answered_with_a_ta1_naming_the_duplicate(self):
        self.mailbox(ACME, leave=False)
        again = self.send(self.payload)
        self.assertEqual([q["code"] for q in again["queued"]], ["TA1"])
        segment = self.ta1()
        self.assertEqual(segment[1], "000000077")   # the control number replayed
        self.assertEqual(segment[4], "R")           # rejected
        self.assertEqual(segment[5], "025")         # duplicate control number

    def test_no_997_is_sent_for_a_refused_envelope(self):
        self.mailbox(ACME, leave=False)
        self.send(self.payload)
        self.assertEqual(self.mailbox(ACME, "acknowledgment"), [])

    def test_the_order_is_not_fulfilled_twice(self):
        self.send(self.payload)
        order = self.order("PO-REPLAY")
        self.assertEqual(len(order["shipments"]), 1)
        self.assertEqual(len(order["invoices"]), 1)

    def test_and_no_further_documents_go_out(self):
        self.mailbox(ACME, leave=False)
        self.send(self.payload)
        for kind in ("response", "despatch", "invoice"):
            self.assertEqual(self.mailbox(ACME, kind), [],
                             "a %s was sent for a refused replay" % kind)

    def test_a_third_copy_is_refused_too(self):
        self.send(self.payload)
        third = self.send(self.payload)
        self.assertFalse(third["accepted"])

    def test_a_new_control_number_is_accepted(self):
        """It is the control number that is refused, not the partner."""
        summary = self.send(x12_order("PO-NEXT"))
        self.assertTrue(summary["accepted"])
        self.assertEqual(summary["orders"], ["PO-NEXT"])

    def test_another_partner_may_use_the_same_number(self):
        """Control numbers are a conversation between two parties, not global."""
        summary = self.send(x12_order("PO-OTHER", sender="GLOBEX",
                                      control="000000077", group="77"))
        self.assertTrue(summary["accepted"])

    def test_the_replay_is_still_archived(self):
        """Refusing it is not forgetting it: the evidence is what a test came for."""
        self.send(self.payload)
        _status, _headers, rows = self.get("/_mock/interchanges")
        inbound = [r for r in rows if r["direction"] == "in"
                   and r["control"] == "000000077"]
        self.assertEqual(len(inbound), 2)


class ReplayingAnEdifactInterchange(MockServerCase):
    def setUp(self):
        super().setUp()
        self.payload = edifact_order("PO-E-REPLAY", control="9001")
        self.send(self.payload, headers=EDIFACT)

    def test_the_second_is_refused(self):
        again = self.send(self.payload, headers=EDIFACT)
        self.assertFalse(again["accepted"])
        self.assertEqual(again["orders"], [])

    def test_the_contrl_rejects_it_with_duplicate_detected(self):
        self.mailbox(EURODIS, leave=False)
        self.send(self.payload, headers=EDIFACT)
        message = self.document(EURODIS, "acknowledgment").groups[0].messages[0]
        uci = message.find("UCI")
        self.assertEqual(uci.get(1), "9001")     # the reference replayed
        self.assertEqual(uci.get(4), "4")        # this level and all below rejected
        self.assertEqual(uci.get(5), "26")       # duplicate detected
        self.assertEqual(uci.get(6), "UNB")      # the segment at fault

    def test_no_ucm_is_given_for_a_refused_envelope(self):
        self.mailbox(EURODIS, leave=False)
        self.send(self.payload, headers=EDIFACT)
        message = self.document(EURODIS, "acknowledgment").groups[0].messages[0]
        self.assertEqual(message.find_all("UCM"), [])

    def test_the_order_is_not_fulfilled_twice(self):
        self.send(self.payload, headers=EDIFACT)
        order = self.order("PO-E-REPLAY")
        self.assertEqual(len(order["shipments"]), 1)
        self.assertEqual(len(order["invoices"]), 1)

    def test_a_new_reference_is_accepted(self):
        summary = self.send(edifact_order("PO-E-NEXT"), headers=EDIFACT)
        self.assertTrue(summary["accepted"])


class AllowingDuplicates(MockServerCase):
    """`--allow-duplicates` restores the older behaviour for tests that want it."""

    config_kwargs = {"allow_duplicates": True}

    def test_a_replay_is_accepted(self):
        payload = x12_order("PO-ALLOWED", control="000000077", group="77")
        self.send(payload)
        again = self.send(payload)
        self.assertTrue(again["accepted"])
        self.assertEqual(again["orders"], ["PO-ALLOWED"])

    def test_and_no_ta1_is_sent(self):
        payload = x12_order("PO-ALLOWED2", control="000000077", group="77")
        self.send(payload)
        self.mailbox(ACME, leave=False)
        self.send(payload)
        self.assertEqual(self.mailbox(ACME, "interchange-acknowledgment"), [])


class AcknowledgmentsAreNotThemselvesOutstanding(MockServerCase):
    """Nobody acknowledges a 997, and nobody acknowledges a TA1 either."""

    def test_a_ta1_does_not_appear_as_unacknowledged(self):
        payload = x12_order("PO-TA1", control="000000077", group="77")
        self.send(payload)
        self.send(payload)                      # produces a TA1
        _status, _headers, rows = self.get("/_mock/unacknowledged")
        kinds = {r["kind"] for r in rows}
        self.assertNotIn("interchange-acknowledgment", kinds)
        self.assertNotIn("acknowledgment", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RestatingAnOrderAlreadyFulfilled(MockServerCase):
    """A retry bug sends the order again, not a change (#165).

    #44 refuses a replayed *interchange*. A buyer that retries sends a fresh
    interchange with the same PO number and `BEG01` still `00`, so neither
    that rule nor the restatement path catches it, and replacing an order the
    mock has already fulfilled starts the fulfilment again - it ships and
    bills the whole order twice.

    The line falls at what has *happened*, not at the purpose code alone: an
    order still only `received` may be restated, which is how a change may
    legitimately arrive as an 850, and one already acted on may not.
    """

    def order_again(self, po_number, control="000000091", **kw):
        return self.send(x12_order(po_number, control=control,
                                   group=control[-2:], **kw))

    def test_a_fulfilled_order_cannot_be_restated(self):
        self.send(x12_order("PO-AGAIN"))
        self.assertEqual(self.order("PO-AGAIN")["status"], "invoiced")
        again = self.order_again("PO-AGAIN")
        self.assertEqual([r["reason"] for r in again["refusals"]],
                         ["order number already in use"])
        self.assertEqual(again["orders"], [])

    def test_and_it_is_refused_with_an_855_saying_so(self):
        # An 855 goes back either way, so the assertion is on what it says:
        # "pick another number" is the only answer the sender can act on.
        self.send(x12_order("PO-AGAIN-855"))
        self.mailbox(ACME, leave=False)
        self.order_again("PO-AGAIN-855")
        answers = [row for row in self.mailbox(ACME) if row["code"] == "855"]
        self.assertEqual(len(answers), 1, self.mailbox(ACME))
        self.assertIn("order number already in use", answers[0]["payload"])

    def test_and_nothing_is_shipped_or_billed_a_second_time(self):
        # The point of the whole rule.
        self.send(x12_order("PO-AGAIN-ONCE"))
        before = self.order("PO-AGAIN-ONCE")
        self.order_again("PO-AGAIN-ONCE")
        after = self.order("PO-AGAIN-ONCE")
        self.assertEqual(len(after["shipments"]), len(before["shipments"]))
        self.assertEqual(len(after["invoices"]), len(before["invoices"]))
        self.assertEqual(after["total"], before["total"])

    def test_in_edifact_too(self):
        self.send(edifact_order("PO-EDI-AGAIN", sender=EURODIS), headers=EDIFACT)
        again = self.send(edifact_order("PO-EDI-AGAIN", sender=EURODIS,
                                        control="00000092"), headers=EDIFACT)
        self.assertEqual([r["reason"] for r in again["refusals"]],
                         ["order number already in use"])

    def test_a_change_purpose_is_refused_for_being_a_change_not_a_duplicate(self):
        # The restatement path still owns BEG01 04, and has its own guard:
        # an invoiced order cannot be changed either. This rule does not
        # shadow that one, and the sender is told the accurate reason.
        self.send(x12_order("PO-CHANGED"))
        again = self.order_again("PO-CHANGED", purpose="04",
                                 lines=(("WIDGET-001", 60, "12.50"),))
        self.assertEqual([r["reason"] for r in again["refusals"]],
                         ["the order has been invoiced and can no longer be changed"])


class RestatingAnOrderNothingHasHappenedTo(MockServerCase):
    """The half that stays: an order still only received may be restated."""

    config_kwargs = {"despatch_delay_ms": 3600000, "invoice_delay_ms": 3600000}

    def test_it_replaces_the_order_as_it_always_has(self):
        self.send(x12_order("PO-EARLY"))
        self.assertEqual(self.order("PO-EARLY")["status"], "received")
        again = self.send(x12_order("PO-EARLY", control="000000093", group="93",
                                    lines=(("WIDGET-001", 7, "12.50"),)))
        self.assertEqual(again["refusals"], [])
        self.assertEqual(again["orders"], ["PO-EARLY"])
        self.assertEqual([l["quantity"] for l in self.order("PO-EARLY")["lines"]],
                         ["7"])

    def test_and_a_change_purpose_is_read_as_the_change_it_is(self):
        # BEG01 04 says "this restates the order", which is the path this
        # rule must not disturb.
        self.send(x12_order("PO-EARLY-CHG"))
        again = self.send(x12_order("PO-EARLY-CHG", control="000000095",
                                    group="95", purpose="04",
                                    lines=(("WIDGET-001", 60, "12.50"),)))
        self.assertEqual(again["refusals"], [])
        self.assertEqual(self.order("PO-EARLY-CHG")["lines"][0]["quantity"], "60")


class AllowingDuplicatesCoversThisToo(MockServerCase):
    """A flag that says "send me the same thing twice" has to mean it."""

    config_kwargs = {"allow_duplicates": True}

    def test_a_fulfilled_order_may_still_be_restated(self):
        self.send(x12_order("PO-DUP-OK"))
        again = self.send(x12_order("PO-DUP-OK", control="000000094", group="94"))
        self.assertEqual(again["refusals"], [])
        self.assertEqual(again["orders"], ["PO-DUP-OK"])
