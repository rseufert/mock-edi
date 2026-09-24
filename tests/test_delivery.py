"""Delivering to a partner that has somewhere to receive documents."""
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, MockServerCase, x12_order


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
    @classmethod
    def setUpClass(cls):
        MockServerCase.setUpClass()
        cls.listener = HTTPServer(("127.0.0.1", 0), Listener)
        cls.listener_port = cls.listener.server_address[1]
        cls.listener_thread = threading.Thread(
            target=cls.listener.serve_forever, daemon=True)
        cls.listener_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()
        MockServerCase.tearDownClass()

    def setUp(self):
        MockServerCase.setUp(self)
        Listener.received = []
        Listener.reply_status = 200
        self.url = "http://127.0.0.1:%d/as2" % self.listener_port
        self.patch("/_mock/partners/" + ACME, {"as2_url": self.url})

    def deliver(self):
        self.send(x12_order("PO-PUSH"))
        self.httpd.mock.courier.drain()

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
        self.httpd.mock.courier.drain()
        _s, _h, rows = self.get("/_mock/outbox")
        self.assertTrue(all(row["status"] == "failed" for row in rows))
        self.assertIn("delivery failed", rows[0]["note"])

    def test_a_mailbox_partner_is_left_alone(self):
        self.patch("/_mock/partners/" + ACME, {"as2_url": ""})
        self.send(x12_order("PO-MAILBOX"))
        self.httpd.mock.courier.drain()
        self.assertEqual(Listener.received, [])
        self.assertEqual(len(self.mailbox(ACME)), 4)


class AsynchronousReceipts(MockServerCase):
    @classmethod
    def setUpClass(cls):
        MockServerCase.setUpClass()
        cls.listener = HTTPServer(("127.0.0.1", 0), Listener)
        cls.listener_port = cls.listener.server_address[1]
        cls.listener_thread = threading.Thread(
            target=cls.listener.serve_forever, daemon=True)
        cls.listener_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()
        MockServerCase.tearDownClass()

    def test_the_mdn_is_posted_to_the_url_the_sender_named(self):
        from support import as2_headers
        Listener.received = []
        url = "http://127.0.0.1:%d/mdn" % self.listener_port
        status, _h, _body = self.request(
            "POST", "/as2", x12_order("PO-ASYNC-REAL"),
            headers=as2_headers(async_url=url), raw=True)
        self.assertEqual(status, 202)
        self.httpd.mock.courier.drain()
        self.assertEqual(len(Listener.received), 1)
        body = Listener.received[0]["body"]
        self.assertIn("Disposition: automatic-action", body)
        self.assertIn("Received-Content-MIC:", body)
        _s, _h, rows = self.get("/_mock/mdns")
        self.assertEqual(rows[0]["status"], "sent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
