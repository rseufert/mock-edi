"""Which promise a shipment, an invoice or a sent document kept (#211).

An order can hold two promises of one kind: shipped, then changed to a larger
quantity, it is promised a second despatch. Its timeline then has two
`packed`, two sent 856s and two of each on the billing side, and nothing said
which promise each one kept - a reader could pair them only by kind and by
counting. Every `promised` event now has an id, `promise`, and what was done
in keeping it carries the same number.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, MockServerCase, x12_change, x12_order
from test_document_numbers import TimelineCase, events

# Despatched at once and invoiced an hour later: the window in which a change
# can still add a second consignment.
WINDOW = {"invoice_delay_ms": 3600 * 1000}


class AnOrderWithTwoPromisesOfOneKind(TimelineCase):
    """The order in the issue: 100 shipped, then changed to 150."""
    config_kwargs = WINDOW
    po = "PO-PROMISE"

    def setUp(self):
        super().setUp()
        self.send(x12_order(self.po, lines=(("WIDGET-001", 100, "12.50"),)))
        self.send(x12_change(self.po, [("1", "QI", 150, "12.50")]))
        self.post("/_mock/advance?all")
        self.found = self.timeline(self.po)
        self.promised = {event["promise"]: event["kind"]
                         for event in events(self.found, "promised")}

    def test_it_has_two_despatch_promises_and_each_has_its_own_id(self):
        kinds = sorted(event["kind"] for event in events(self.found, "promised"))
        self.assertEqual(kinds, ["despatch", "despatch", "invoice"])
        self.assertEqual(len(self.promised), 3)
        for promise in self.promised:
            self.assertIsInstance(promise, int)

    def test_the_id_is_the_one_the_scheduled_listing_serves(self):
        _status, _headers, rows = self.get("/_mock/scheduled?all")
        self.assertEqual({row["id"]: row["kind"] for row in rows
                          if row["po_number"] == self.po}, self.promised)

    def test_each_consignment_says_which_despatch_promise_packed_it(self):
        packed = events(self.found, "packed")
        kept = [event["promise"] for event in packed]
        self.assertEqual(len(packed), 2)
        self.assertEqual(len(set(kept)), 2)
        self.assertEqual([self.promised[promise] for promise in kept],
                         ["despatch", "despatch"])

    def test_each_despatch_advice_kept_the_promise_its_consignment_did(self):
        packed = {event["shipment"]: event["promise"]
                  for event in events(self.found, "packed")}
        sent = {event["shipment"]: event["promise"]
                for event in events(self.found, "sent", "856")}
        self.assertEqual(sent, packed)

    def test_each_invoice_and_its_document_name_one_promise_to_invoice(self):
        invoiced = {event["invoice"]: event["promise"]
                    for event in events(self.found, "invoiced")}
        sent = {event["invoice"]: event["promise"]
                for event in events(self.found, "sent", "810")}
        self.assertEqual(len(invoiced), 2)
        self.assertEqual(sent, invoiced)
        for promise in invoiced.values():
            self.assertEqual(self.promised[promise], "invoice")

    def test_everything_done_pairs_with_a_promise_that_exists(self):
        done = (events(self.found, "packed") + events(self.found, "invoiced")
                + events(self.found, "sent", "856")
                + events(self.found, "sent", "810"))
        self.assertEqual(len(done), 8)
        for event in done:
            with self.subTest(event=event["summary"]):
                self.assertIn(event["promise"], self.promised)

    def test_the_documents_listing_and_the_mailbox_have_it_as_a_column(self):
        """Both serve the row whole, so both gain `promise_id`; 0 is none."""
        sent = {event["control"]: event["promise"]
                for event in events(self.found, "sent", "856")}
        _status, _headers, rows = self.get(
            "/_mock/documents?reference=%s&code=856" % self.po)
        self.assertEqual({row["control"]: row["promise_id"] for row in rows},
                         sent)
        collected = self.mailbox(ACME, "despatch")
        self.assertEqual(sorted(row["promise_id"] for row in collected),
                         sorted(sent.values()))
        (response,) = self.mailbox(ACME, "response")
        self.assertEqual(response["promise_id"], 0)

    def test_a_document_sent_on_demand_kept_no_promise(self):
        status, _headers, data = self.post("/_mock/send", {
            "partner": ACME, "kind": "invoice", "order": self.po})
        self.assertEqual(status, 201, data)
        again = events(self.timeline(self.po), "sent", "810")[-1]
        self.assertIn("promise", again)
        self.assertIsNone(again["promise"])

    def test_an_answer_is_not_a_promise_kept(self):
        """The 855 answers the order at once; nothing was promised first."""
        (sent,) = events(self.found, "sent", "855")
        self.assertIsNone(sent["promise"])

    def test_an_acknowledgment_and_a_received_document_have_no_such_field(self):
        for event in (events(self.found, "sent", "997")
                      + events(self.found, "received")):
            with self.subTest(event=event["summary"]):
                self.assertNotIn("promise", event)


class ASellerThatBillsBeforeItDespatches(TimelineCase):
    """`out-of-order`: the invoice comes due first, and packs to have
    something to bill. The consignment was packed in keeping the invoice's
    promise, and the 856 sent later keeps the despatch's."""

    def test_each_event_names_the_promise_in_whose_keeping_it_happened(self):
        self.patch("/_mock/partners/ACME", {"behaviour": "out-of-order"})
        self.send(x12_order("PO-OOO"))
        found = self.timeline("PO-OOO")
        promised = {event["kind"]: event["promise"]
                    for event in events(found, "promised")}
        (packed,) = events(found, "packed")
        (invoiced,) = events(found, "invoiced")
        (advice,) = events(found, "sent", "856")
        self.assertEqual(packed["promise"], promised["invoice"])
        self.assertEqual(invoiced["promise"], promised["invoice"])
        self.assertEqual(advice["promise"], promised["despatch"])


class ARowFromBeforePromisesWereRecorded(TimelineCase):
    def test_it_says_null_and_not_a_guess(self):
        self.send(x12_order("PO-OLD-P"))
        with self.httpd.mock.lock:
            for table in ("shipment", "invoice", "transaction_set"):
                self.httpd.mock.conn.execute(
                    "UPDATE %s SET promise_id = 0" % table)
            self.httpd.mock.conn.commit()
        found = self.timeline("PO-OLD-P")
        for event in (events(found, "packed") + events(found, "invoiced")
                      + events(found, "sent", "856")
                      + events(found, "sent", "810")):
            with self.subTest(event=event["summary"]):
                self.assertIsNone(event["promise"])
        for event in events(found, "promised"):
            self.assertIsInstance(event["promise"], int)


if __name__ == "__main__":
    unittest.main()
