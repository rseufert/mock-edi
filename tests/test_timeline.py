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

from support import (ACME, EURODIS, INITECH, MockServerCase, acknowledge,
                     edifact_order, x12_order)

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


if __name__ == "__main__":
    unittest.main()
