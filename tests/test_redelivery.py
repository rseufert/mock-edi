"""A delivery that failed can be tried again, unchanged.

A partner's listener restarts between the 997 and the 855. The 997 fails;
the rest arrive when the listener is back. The partner now holds a response,
a ship notice and an invoice for an order whose acknowledgment it never got,
and nothing the mock offers puts that right: `/_mock/send` builds a *new*
document with a *new* control number, which is a different event on the wire.

A retry is the same bytes and the same control numbers going out a second
time. That is what a real sender's AS2 software does, and it is the only way
to test the thing a listener must get right - being idempotent about a
control number it has already seen.

Nothing retries on a timer here. A test that wants a retry asks for one.
"""
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, MockServerCase, x12_order


def isa13(body):
    """The interchange control number of a posted X12 document, unpadded.

    ISA13 is fixed at nine characters on the wire and the outbox row keeps
    the number, so `000000001` and `1` are the same interchange.
    """
    first = body.replace("\n", "").split("~")[0]
    if not first.startswith("ISA"):
        return ""
    return first.split("*")[13].lstrip("0") or "0"


class FlakyListener(BaseHTTPRequestHandler):
    """A partner whose listener refuses the first N posts it is given."""
    received = []
    refuse_first = 0
    reply_body = (b"Reporting-UA: partner\r\n"
                  b"Disposition: automatic-action/MDN-sent-automatically; processed\r\n")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        FlakyListener.received.append({"headers": self.headers, "body": body})
        if FlakyListener.refuse_first > 0:
            FlakyListener.refuse_first -= 1
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/report")
        self.send_header("Content-Length", str(len(FlakyListener.reply_body)))
        self.end_headers()
        self.wfile.write(FlakyListener.reply_body)

    def log_message(self, *args):
        pass


class OutboxCase(MockServerCase):
    def outbox(self):
        _status, _headers, rows = self.get("/_mock/outbox")
        return rows

    def failed(self):
        return [row for row in self.outbox() if row["status"] == "failed"]


class RedeliveringAFailedDocument(OutboxCase):
    config_kwargs = {"deliver_timeout": 3.0}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.listener = HTTPServer(("127.0.0.1", 0), FlakyListener)
        cls.listener_port = cls.listener.server_address[1]
        cls.listener_thread = threading.Thread(
            target=cls.listener.serve_forever, daemon=True)
        cls.listener_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        FlakyListener.received = []
        FlakyListener.refuse_first = 0
        self.patch("/_mock/partners/" + ACME,
                   {"as2_url": "http://127.0.0.1:%d/as2" % self.listener_port})

    def test_the_first_delivery_is_recorded_as_failed(self):
        FlakyListener.refuse_first = 1
        self.send(x12_order("RETRY-A"))
        self.settle()
        self.assertEqual(len(self.failed()), 1)
        self.assertIn("503", self.failed()[0]["note"])

    def test_a_failed_document_can_be_delivered_again(self):
        FlakyListener.refuse_first = 1
        self.send(x12_order("RETRY-B"))
        self.settle()
        failed = self.failed()[0]
        status, _headers, data = self.post(
            "/_mock/outbox/%d/retry" % failed["id"])
        self.assertEqual(status, 200, data)
        self.assertEqual(data["retried"], [failed["id"]])
        self.settle()
        rows = {row["id"]: row for row in self.outbox()}
        self.assertEqual(rows[failed["id"]]["status"], "delivered")

    def test_the_listener_sees_the_same_control_number_twice(self):
        # The thing a real listener has to get right, and the test that
        # could not be written before: /_mock/send would have produced a
        # different control number and proved nothing.
        FlakyListener.refuse_first = 1
        self.send(x12_order("RETRY-SAME"))
        self.settle()
        failed = self.failed()[0]
        self.post("/_mock/outbox/%d/retry" % failed["id"])
        self.settle()
        bodies = [item["body"] for item in FlakyListener.received
                  if isa13(item["body"]) == failed["control"].lstrip("0")]
        self.assertEqual(len(bodies), 2, "the document went out once, not twice")
        self.assertEqual(bodies[0], bodies[1], "the retry was not byte for byte")

    def test_the_attempt_history_is_on_the_row(self):
        FlakyListener.refuse_first = 1
        self.send(x12_order("RETRY-HISTORY"))
        self.settle()
        failed = self.failed()[0]
        self.assertEqual(failed["attempts"], 1)
        self.assertIn("503", failed["last_error"])
        self.assertTrue(failed["last_attempt_at"])

        self.post("/_mock/outbox/%d/retry" % failed["id"])
        self.settle()
        row = {r["id"]: r for r in self.outbox()}[failed["id"]]
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["last_error"], "",
                         "a retry that succeeded still carries an error")

    def test_everything_that_failed_can_be_retried_at_once(self):
        # The scenario from the issue: the listener is down for the whole
        # order, and comes back.
        FlakyListener.refuse_first = 4
        self.send(x12_order("RETRY-ALL"))
        self.settle()
        self.assertEqual(len(self.failed()), 4)

        status, _headers, data = self.post("/_mock/advance?failed")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["count"], 4)
        self.settle()
        self.assertEqual(self.failed(), [])

    def test_they_go_out_in_the_order_they_were_queued(self):
        FlakyListener.refuse_first = 4
        self.send(x12_order("RETRY-ORDER"))
        self.settle()
        FlakyListener.received = []
        self.post("/_mock/advance?failed")
        self.settle()
        codes = []
        for item in FlakyListener.received:
            subject = item["headers"].get("Subject") or ""
            codes.append(subject.split()[0] if subject else "?")
        self.assertEqual(codes, ["997", "855", "856", "810"])

    def test_only_one_partners_failures_are_retried(self):
        FlakyListener.refuse_first = 4
        self.send(x12_order("RETRY-MINE"))
        self.settle()
        _status, _headers, data = self.post("/_mock/advance?failed&partner=NOBODY")
        self.assertEqual(data["count"], 0)
        self.assertEqual(len(self.failed()), 4)


class WhatCannotBeRetried(OutboxCase):
    """Retrying is for a failure, not for a do-over."""

    def test_a_delivered_document_is_refused(self):
        self.send(x12_order("RETRY-NO"))
        rows = self.outbox()
        status, _headers, data = self.post("/_mock/outbox/%d/retry" % rows[0]["id"])
        self.assertEqual(status, 409)
        self.assertIn("not failed", data["error"])

    def test_an_unknown_document_is_a_404(self):
        status, _headers, data = self.post("/_mock/outbox/999999/retry")
        self.assertEqual(status, 404)
        self.assertIn("999999", data["error"])

    def test_a_non_numeric_id_is_a_404(self):
        status, _headers, _data = self.post("/_mock/outbox/abc/retry")
        self.assertEqual(status, 404)

    def test_get_is_not_how_you_retry(self):
        status, _headers, _data = self.get("/_mock/outbox/1/retry")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
