"""Delivering to a partner that has somewhere to receive documents."""
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import sqlite3

from support import ACME, FileDatabaseCase, MockServerCase, as2_headers, parse, x12_order


class Listener(BaseHTTPRequestHandler):
    """A trading partner's AS2 endpoint, as small as one can be."""
    received = []
    reply_status = 200
    reply_body = (b"Reporting-UA: partner\r\n"
                  b"Disposition: automatic-action/MDN-sent-automatically; processed\r\n")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        Listener.received.append({
            # Kept as the parsed message, not a dict: HTTP header names are
            # case-insensitive and a plain dict is not, which would make this
            # listener stricter than a real one.
            "headers": self.headers,
            "names": list(self.headers.keys()),
            "body": self.rfile.read(length).decode("utf-8", "replace"),
        })
        self.send_response(Listener.reply_status)
        self.send_header("Content-Type", "multipart/report")
        self.send_header("Content-Length", str(len(Listener.reply_body)))
        self.end_headers()
        self.wfile.write(Listener.reply_body)

    def log_message(self, *args):
        pass


class DeliveryToAPartner(MockServerCase):
    # A short delivery timeout so that the "nobody is listening" case is
    # bounded on platforms where a refused connection is not instant.
    config_kwargs = {"deliver_timeout": 3.0}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.listener = HTTPServer(("127.0.0.1", 0), Listener)
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
        Listener.received = []
        Listener.reply_status = 200
        self.url = "http://127.0.0.1:%d/as2" % self.listener_port
        self.patch("/_mock/partners/" + ACME, {"as2_url": self.url})

    def deliver(self):
        self.send(x12_order("PO-PUSH"))
        self.settle()

    def test_documents_are_posted_rather_than_waiting_to_be_collected(self):
        self.deliver()
        self.assertEqual(len(Listener.received), 4)
        self.assertTrue(Listener.received[0]["body"].startswith("ISA*00*"))

    def test_they_arrive_in_the_order_they_were_queued(self):
        self.deliver()
        subjects = [item["headers"]["Subject"] for item in Listener.received]
        self.assertEqual([s.split()[0] for s in subjects],
                         ["997", "855", "856", "810"])

    def test_the_as2_headers_name_both_sides(self):
        self.deliver()
        headers = Listener.received[0]["headers"]
        self.assertEqual(headers["AS2-From"], "MOCKEDI")
        self.assertEqual(headers["AS2-To"], ACME)
        self.assertEqual(headers["Content-Type"], "application/edi-x12")
        self.assertTrue(headers["Message-ID"].startswith("<"))

    def test_the_header_names_go_out_spelled_the_way_as2_spells_them(self):
        self.deliver()
        names = Listener.received[0]["names"]
        self.assertIn("AS2-From", names)
        self.assertIn("AS2-To", names)
        self.assertIn("Message-ID", names)

    def test_a_receipt_is_requested(self):
        self.deliver()
        headers = Listener.received[0]["headers"]
        self.assertIn("Disposition-Notification-To", headers)
        self.assertIn("sha256", headers["Disposition-Notification-Options"])

    def test_the_outbox_records_the_delivery_and_the_receipt(self):
        self.deliver()
        _s, _h, rows = self.get("/_mock/outbox")
        self.assertTrue(all(row["status"] == "delivered" for row in rows), rows)
        self.assertTrue(all("processed" in row["note"] for row in rows))
        self.assertEqual(rows[0]["delivery"], self.url)

    def test_the_partners_receipt_is_recorded_as_an_incoming_mdn(self):
        self.deliver()
        _s, _h, rows = self.get("/_mock/mdns")
        self.assertTrue(rows)
        self.assertTrue(all(row["direction"] == "in" for row in rows))

    def test_a_partner_that_refuses_leaves_the_document_failed(self):
        Listener.reply_status = 503
        self.deliver()
        _s, _h, rows = self.get("/_mock/outbox")
        self.assertTrue(all(row["status"] == "failed" for row in rows))
        self.assertIn("503", rows[0]["note"])

    def test_a_partner_that_is_not_listening_is_reported_not_raised(self):
        self.patch("/_mock/partners/" + ACME,
                   {"as2_url": "http://127.0.0.1:1/nowhere"})
        self.send(x12_order("PO-NOWHERE"))
        rows = self.settle()
        self.assertTrue(all(row["status"] == "failed" for row in rows))
        self.assertIn("delivery failed", rows[0]["note"])

    def test_a_mailbox_partner_is_left_alone(self):
        self.patch("/_mock/partners/" + ACME, {"as2_url": ""})
        self.send(x12_order("PO-MAILBOX"))
        self.httpd.mock.courier.drain(5.0)
        self.assertEqual(Listener.received, [])
        self.assertEqual(len(self.mailbox(ACME)), 4)


class AsynchronousReceipts(MockServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.listener = HTTPServer(("127.0.0.1", 0), Listener)
        cls.listener_port = cls.listener.server_address[1]
        cls.listener_thread = threading.Thread(
            target=cls.listener.serve_forever, daemon=True)
        cls.listener_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()
        super().tearDownClass()

    def test_the_mdn_is_posted_to_the_url_the_sender_named(self):
        from support import as2_headers
        Listener.received = []
        url = "http://127.0.0.1:%d/mdn" % self.listener_port
        status, _h, _body = self.request(
            "POST", "/as2", x12_order("PO-ASYNC-REAL"),
            headers=as2_headers(async_url=url), raw=True)
        self.assertEqual(status, 202)
        self.httpd.mock.courier.drain(20.0)
        self.assertEqual(len(Listener.received), 1)
        body = Listener.received[0]["body"]
        self.assertIn("Disposition: automatic-action", body)
        self.assertIn("Received-Content-MIC:", body)
        _s, _h, rows = self.get("/_mock/mdns")
        self.assertEqual(rows[0]["status"], "sent")



class AfterARestart(FileDatabaseCase):
    """Documents left ready by one run are delivered by the next.

    The courier learns of work when it is released. A run that stopped with
    documents still ready - collected by nobody, posted by nobody - used to
    leave them there for good.
    """

    config_kwargs = {"deliver_timeout": 3.0}

    def setUp(self):
        super().setUp()
        Listener.received = []
        Listener.reply_status = 200
        self.listener = HTTPServer(("127.0.0.1", 0), Listener)
        threading.Thread(target=self.listener.serve_forever, daemon=True).start()
        self.addCleanup(self.listener.server_close)
        self.addCleanup(self.listener.shutdown)
        self.url = "http://127.0.0.1:%d/as2" % self.listener.server_address[1]

    def leave_ready(self):
        """An order answered while ACME had nowhere to post to, then an URL."""
        self.send(x12_order("PO-RESTART"))
        self.patch("/_mock/partners/" + ACME, {"as2_url": self.url})
        _status, _headers, rows = self.get("/_mock/outbox")
        self.assertEqual({r["status"] for r in rows}, {"ready"})
        self.assertEqual(Listener.received, [])
        # The outbox lists newest first; delivery is in the order queued.
        return [r["code"] for r in sorted(rows, key=lambda r: r["id"])]

    def test_what_was_ready_is_delivered_in_order(self):
        codes = self.leave_ready()
        self.restart()
        rows = self.settle()
        self.assertEqual({r["status"] for r in rows}, {"delivered"})
        self.assertEqual(len(Listener.received), len(codes))
        self.assertEqual([parse(item["body"]).codes()[0]
                          for item in Listener.received], codes)

    def test_a_partner_with_nowhere_to_post_to_keeps_its_documents(self):
        self.send(x12_order("PO-COLLECT"))
        self.restart()
        self.httpd.mock.courier.drain(5.0)
        self.assertEqual(Listener.received, [])
        self.assertEqual(len(self.mailbox(ACME)), 4)

    def test_an_asynchronous_mdn_left_pending_is_posted(self):
        # The run that received the message died before posting its MDN: the
        # row is written, the post never happened.
        self.post("/as2", x12_order("PO-ASYNC"),
                  headers=as2_headers(async_url=self.url))
        self.httpd.mock.courier.drain(5.0)
        self.stop()
        Listener.received = []
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE mdn SET status = 'pending' WHERE mode = 'async'")
        conn.commit()
        conn.close()

        self.start()
        self.httpd.mock.courier.drain(5.0)
        posted = [item for item in Listener.received
                  if "disposition-notification" in item["headers"].get("Content-Type", "")]
        self.assertEqual(len(posted), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
