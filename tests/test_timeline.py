"""One order's whole conversation, in the order it happened.

Four endpoints held the answer to "why did my 810 not match" and none of them
held it alone: `/_mock/orders` for the state, `/_mock/documents` for what was
archived, `/_mock/outbox` for what was delivered, `/_mock/unacknowledged` for
what is still owed. Assembling and sorting them was left to whoever was
debugging, which is the wrong way round for a mock that exists to be debugged
against.

Nothing new is recorded. These tests are about the sequence being right, and
staying right when several things happen inside one second - which, with
second-precision timestamps, is nearly always.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import (ACME, EURODIS, INITECH, MockServerCase, STEPS,
                     acknowledge, edifact_order, spans_more_than_a_second,
                     stepping, x12_order)

EDIFACT = {"Content-Type": "application/edifact"}


class TheTimelineOfAPlainOrder(MockServerCase):

    def timeline(self, po_number, query=""):
        status, _headers, data = self.get(
            "/_mock/orders/%s/timeline%s" % (po_number, query))
        self.assertEqual(status, 200, data)
        return data

    def kinds(self, po_number):
        return [event["event"] for event in self.timeline(po_number)["events"]]

    def test_it_names_the_order_and_where_it_got_to(self):
        self.send(x12_order("TL-A"))
        found = self.timeline("TL-A")
        self.assertEqual(found["order"], "TL-A")
        self.assertEqual(found["partner"], ACME)
        self.assertEqual(found["status"], "invoiced")

    def test_the_order_arrives_before_anything_is_answered(self):
        self.send(x12_order("TL-B"))
        kinds = self.kinds("TL-B")
        self.assertEqual(kinds[0], "received")
        self.assertEqual(kinds[1], "ordered")
        self.assertLess(kinds.index("ordered"), kinds.index("sent"))

    def test_the_work_is_promised_before_it_is_done(self):
        self.send(x12_order("TL-C"))
        kinds = self.kinds("TL-C")
        self.assertLess(kinds.index("promised"), kinds.index("packed"))
        self.assertLess(kinds.index("packed"), kinds.index("invoiced"))

    def test_every_document_both_ways_is_there(self):
        self.send(x12_order("TL-D"))
        events = self.timeline("TL-D")["events"]
        codes = [(event["direction"], event["code"])
                 for event in events if event["event"] in ("received", "sent")]
        self.assertEqual(codes, [("in", "850"), ("out", "997"), ("out", "855"),
                                 ("out", "856"), ("out", "810")])

    def test_the_997_for_the_order_is_found_through_its_envelope(self):
        # Its reference is the interchange it answers, not the order inside,
        # so it is only here if the envelope was followed.
        self.send(x12_order("TL-E"))
        sent = [event for event in self.timeline("TL-E")["events"]
                if event.get("code") == "997"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["direction"], "out")

    def test_each_event_carries_a_line_of_prose(self):
        self.send(x12_order("TL-F"))
        for event in self.timeline("TL-F")["events"]:
            self.assertTrue(event["summary"], event)

    def test_the_order_is_summarised_with_its_lines_and_total(self):
        self.send(x12_order("TL-G"))
        ordered = [event for event in self.timeline("TL-G")["events"]
                   if event["event"] == "ordered"][0]
        self.assertEqual(ordered["lines"], 2)
        self.assertIn("1416.00", ordered["summary"])

    def test_timestamps_never_go_backwards(self):
        self.send(x12_order("TL-H"))
        stamps = [event["at"] for event in self.timeline("TL-H")["events"]]
        self.assertEqual(stamps, sorted(stamps))

    def test_the_order_is_the_same_on_a_second_call(self):
        # Several events share one second, so the tie-break has to be stable
        # or the timeline would wobble between calls.
        self.send(x12_order("TL-I"))
        first = [event["summary"] for event in self.timeline("TL-I")["events"]]
        second = [event["summary"] for event in self.timeline("TL-I")["events"]]
        self.assertEqual(first, second)

    def test_payloads_are_left_out_unless_asked_for(self):
        self.send(x12_order("TL-J"))
        events = self.timeline("TL-J")["events"]
        self.assertNotIn("payload", events[0])
        with_raw = self.timeline("TL-J", "?raw")["events"]
        received = [event for event in with_raw
                    if event["event"] == "received"][0]
        self.assertIn("ISA*", received["payload"])

    def test_an_unknown_order_is_a_404(self):
        status, _headers, data = self.get("/_mock/orders/NOPE/timeline")
        self.assertEqual(status, 404)
        self.assertIn("NOPE", data["error"])


class WhatTheFindingsLookLike(MockServerCase):

    def timeline(self, po_number):
        _status, _headers, data = self.get("/_mock/orders/%s/timeline" % po_number)
        return data

    def test_a_rejected_line_is_visible_on_the_order(self):
        self.send(x12_order("TL-REJ", sender=INITECH))
        found = self.timeline("TL-REJ")
        self.assertEqual(found["partner"], INITECH)
        self.assertIn("packed", [event["event"] for event in found["events"]])

    def test_a_finding_on_an_inbound_document_is_carried(self):
        from mockedi.envelope import seg
        self.send(x12_order("TL-FIND", extra=[seg("REF", "ZZ", "x" * 40)]))
        received = [event for event in self.timeline("TL-FIND")["events"]
                    if event["event"] == "received"][0]
        self.assertTrue(received["findings"], received)
        self.assertIn("finding", received["summary"])


class WhenTheWorkIsPostponed(MockServerCase):
    config_kwargs = {"invoice_delay_ms": 3600 * 1000}

    def test_a_promise_not_yet_kept_says_so(self):
        self.send(x12_order("TL-LATE"))
        _status, _headers, found = self.get("/_mock/orders/TL-LATE/timeline")
        promised = [event for event in found["events"]
                    if event["event"] == "promised" and event["kind"] == "invoice"][0]
        self.assertEqual(promised["doneAt"], "")
        self.assertIn("not done yet", promised["summary"])

    def test_and_there_is_no_invoice_event_yet(self):
        self.send(x12_order("TL-LATE-2"))
        _status, _headers, found = self.get("/_mock/orders/TL-LATE-2/timeline")
        self.assertNotIn("invoiced",
                         [event["event"] for event in found["events"]])


class WhenThePartnerAnswers(MockServerCase):

    def test_a_receipt_becomes_an_acknowledged_event_at_its_own_time(self):
        self.send(x12_order("TL-ACK"))
        response = self.mailbox(ACME, "response")[0]["payload"]
        self.send(acknowledge(response))
        _status, _headers, found = self.get("/_mock/orders/TL-ACK/timeline")
        events = found["events"]
        acked = [event for event in events if event["event"] == "acknowledged"]
        self.assertEqual(len(acked), 1)
        self.assertEqual(acked[0]["code"], "855")
        self.assertEqual(acked[0]["status"], "accepted")
        # After the 855 it answers, which is the point of a timeline.
        sent = [i for i, event in enumerate(events)
                if event["event"] == "sent" and event["code"] == "855"][0]
        self.assertGreater(events.index(acked[0]), sent)


class TheEdifactSide(MockServerCase):

    def test_an_orders_reads_the_same_way(self):
        self.send(edifact_order("TL-EDI"), headers=EDIFACT)
        _status, _headers, found = self.get("/_mock/orders/TL-EDI/timeline")
        self.assertEqual(found["partner"], EURODIS)
        codes = [(event["direction"], event["code"])
                 for event in found["events"]
                 if event["event"] in ("received", "sent")]
        self.assertEqual(codes, [("in", "ORDERS"), ("out", "CONTRL"),
                                 ("out", "ORDRSP"), ("out", "DESADV"),
                                 ("out", "INVOIC")])


def labels(found):
    """Each event as one word, with the document or the work it is about.

    A list of these is the whole assertion these tests make: the sequence,
    and nothing else.
    """
    out = []
    for event in found["events"]:
        kind = event["event"]
        if kind in ("received", "sent", "acknowledged"):
            out.append("%s %s" % (kind, event["code"]))
        elif kind == "promised":
            out.append("promised %s" % event["kind"])
        else:
            out.append(kind)
    return out


# What a plain order actually does, in the order it does it: the 850 arrives
# and is recorded, the 997 and the 855 answer it, the work is promised and
# then done, and each document goes out beside the work that produced it.
# Before #195 every `sent` landed after the packing and the invoicing,
# because the rank by kind of event said so.
PLAIN = ["received 850", "ordered", "sent 997", "sent 855",
         "promised despatch", "promised invoice",
         "packed", "sent 856", "invoiced", "sent 810"]


class TheOrderInsideOneSecond(MockServerCase):
    """Timestamps are second-precision, so this is nearly every order (#195)."""

    def timeline(self, po_number):
        status, _headers, data = self.get("/_mock/orders/%s/timeline" % po_number)
        self.assertEqual(status, 200, data)
        return data

    def test_a_plain_order_reads_in_the_order_it_happened(self):
        self.send(x12_order("TL-SEQ"))
        found = self.timeline("TL-SEQ")
        self.assertEqual(labels(found), PLAIN)
        # The premise: all of it inside one second, so nothing but the
        # recorded sequence can be putting it in this order.
        self.assertEqual(len({event["at"] for event in found["events"]}), 1)

    def test_the_sequence_is_the_same_on_a_second_call(self):
        self.send(x12_order("TL-SEQ-2"))
        self.assertEqual(labels(self.timeline("TL-SEQ-2")),
                         labels(self.timeline("TL-SEQ-2")))

    def test_an_edifact_order_reads_the_same_way(self):
        self.send(edifact_order("TL-SEQ-EDI"), headers=EDIFACT)
        self.assertEqual(
            labels(self.timeline("TL-SEQ-EDI")),
            ["received ORDERS", "ordered", "sent CONTRL", "sent ORDRSP",
             "promised despatch", "promised invoice",
             "packed", "sent DESADV", "invoiced", "sent INVOIC"])


class TheReleasedSecond(MockServerCase):
    """Work held back by a delay, and then all let go at once.

    The release writes every document that is due in one go, so a sequence
    taken there would put the 856 and the 810 after the invoice raised
    between them. The number is taken when the document is queued instead.
    """
    config_kwargs = {"despatch_delay_ms": 3600 * 1000,
                     "invoice_delay_ms": 3600 * 1000}

    def test_each_document_stays_with_the_work_that_produced_it(self):
        self.send(x12_order("TL-HELD"))
        self.post("/_mock/advance?all")
        status, _headers, found = self.get("/_mock/orders/TL-HELD/timeline")
        self.assertEqual(status, 200, found)
        self.assertEqual(labels(found), PLAIN)
        released = {event["at"] for event in found["events"]
                    if event["event"] in ("packed", "invoiced")
                    or event.get("code") in ("856", "810")}
        self.assertEqual(len(released), 1, "the four should share one second")


class WhenTheSecondTicksWhileAnOrderIsHandled(MockServerCase):
    """#238: the order has to hold whichever second each step falls in.

    Until this was fixed a sent document took its sequence when it was
    queued and its `at` when the release loop reached it, a few calls later.
    Any second boundary in between and `at` - which sorts first - put the
    document after work that was sequenced before it. Eleven of these twelve
    steps came back wrong, including "packed and invoiced before the 997 was
    sent", which is the sentence #195 was filed to remove.
    """

    def timeline(self, po_number):
        status, _headers, data = self.get("/_mock/orders/%s/timeline" % po_number)
        self.assertEqual(status, 200, data)
        return data

    def test_a_plain_order_reads_in_order_at_every_step(self):
        crossed = 0
        for step in STEPS:
            with self.subTest(step=step):
                self.post("/_mock/reset")
                with stepping(step):
                    self.send(x12_order("PO-STEP"))
                    found = self.timeline("PO-STEP")
                self.assertEqual(labels(found), PLAIN)
                crossed += spans_more_than_a_second(found)
        # The premise: the clock really is crossing seconds mid-order. Without
        # this the test could pass by never reproducing the bug at all.
        self.assertGreater(crossed, len(STEPS) // 2,
                           "the stepped clock is not crossing seconds")

    def test_the_times_never_run_backwards(self):
        for step in STEPS:
            with self.subTest(step=step):
                self.post("/_mock/reset")
                with stepping(step):
                    self.send(x12_order("PO-STEP-AT"))
                    found = self.timeline("PO-STEP-AT")
                stamps = [event["at"] for event in found["events"]]
                self.assertEqual(stamps, sorted(stamps), stamps)


class WhenTheSecondTicksDuringARelease(MockServerCase):
    """The same, for work held back and then let go by `advance`.

    A release writes every document that is due in one go, so this is where
    the gap between queueing and releasing is widest.
    """

    config_kwargs = {"despatch_delay_ms": 3600 * 1000,
                     "invoice_delay_ms": 3600 * 1000}

    def test_the_released_documents_keep_their_place(self):
        crossed = 0
        for step in STEPS:
            with self.subTest(step=step):
                self.post("/_mock/reset")
                with stepping(step):
                    self.send(x12_order("PO-STEP-REL"))
                    self.post("/_mock/advance?all")
                    status, _headers, found = self.get(
                        "/_mock/orders/PO-STEP-REL/timeline")
                self.assertEqual(status, 200, found)
                self.assertEqual(labels(found), PLAIN)
                stamps = [event["at"] for event in found["events"]]
                self.assertEqual(stamps, sorted(stamps), stamps)
                crossed += spans_more_than_a_second(found)
        self.assertGreater(crossed, len(STEPS) // 2,
                           "the stepped clock is not crossing seconds")


# Every GET the index advertises, so that a route added later cannot put the
# sequence back into a response by returning rows whole.
PUBLIC_GETS = ("/_mock/health", "/_mock/state", "/_mock/partners",
               "/_mock/catalog", "/_mock/orders", "/_mock/orders/TL-PUBLIC",
               "/_mock/disagreements", "/_mock/remittances",
               "/_mock/documents", "/_mock/documents/1",
               "/_mock/interchanges", "/_mock/interchanges/1",
               "/_mock/mailbox?leave", "/_mock/orders/TL-PUBLIC/timeline",
               "/_mock/outbox", "/_mock/scheduled?all", "/_mock/drop",
               "/_mock/mdns", "/_mock/unacknowledged",
               "/_mock/dictionary/X12/850")


def sequences_in(blob, trail=""):
    """Every `seq` or `ack_seq` anywhere in a response, by where it is."""
    found = []
    if isinstance(blob, dict):
        for key, value in blob.items():
            if key in ("seq", "ack_seq"):
                found.append("%s.%s" % (trail, key))
            found += sequences_in(value, "%s.%s" % (trail, key))
    elif isinstance(blob, list):
        for index, value in enumerate(blob):
            found += sequences_in(value, "%s[%d]" % (trail, index))
    return found


class TheSequenceIsTheMocksOwnBusiness(MockServerCase):
    """It orders the timeline and belongs in no response (#195).

    `/_mock/orders`, `/_mock/documents` and `/_mock/mailbox` all hand back
    rows from these tables whole, so a new column lands in each of them
    unless something drops it. One of the three was missed when this was
    written, which is why the check is every endpoint at once rather than
    the three that were known about.
    """

    def test_no_endpoint_returns_it(self):
        self.send(x12_order("TL-PUBLIC"))
        self.send(acknowledge(
            self.mailbox(ACME, "response", leave=True)[0]["payload"]))
        leaks = {}
        for path in PUBLIC_GETS:
            status, _headers, data = self.get(path)
            if status != 200:
                continue
            found = sequences_in(data)
            if found:
                leaks[path] = found
        self.assertEqual(leaks, {})

    def test_but_the_timeline_is_still_ordered_by_it(self):
        # The other half of the claim: dropped from the responses, and doing
        # its job in the one place it exists for.
        self.send(x12_order("TL-PUBLIC-2"))
        _status, _headers, found = self.get("/_mock/orders/TL-PUBLIC-2/timeline")
        self.assertEqual(labels(found), PLAIN)



if __name__ == "__main__":
    unittest.main()
