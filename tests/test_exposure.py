"""Running where others can reach it: the warning, --deliver-to, and --auth (#29)."""
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import delivery
from mockedi.__main__ import exposure_warnings
from mockedi.server import Config

from support import ACME, MockServerCase, as2_headers, x12_order

METADATA = "http://169.254.169.254/latest/meta-data"


class TheStartupWarning(unittest.TestCase):
    def test_loopback_is_quiet(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertEqual(exposure_warnings(Config(host=host)), [], host)

    def test_every_interface_without_auth_names_the_flag(self):
        warnings = exposure_warnings(Config(host="0.0.0.0", port=8080))
        self.assertIn("--auth", warnings[0])
        self.assertIn("0.0.0.0", warnings[0])
        self.assertIn("8080", warnings[0])

    def test_a_host_name_is_assumed_reachable(self):
        self.assertTrue(exposure_warnings(Config(host="mock.internal")))

    def test_auth_and_an_allowlist_silence_it(self):
        self.assertEqual(exposure_warnings(Config(
            host="0.0.0.0", basic_auth="u:p", deliver_to="partner.example")), [])

    def test_without_an_allowlist_the_courier_is_mentioned(self):
        warnings = exposure_warnings(Config(host="0.0.0.0", basic_auth="u:p"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("--deliver-to", warnings[0])


class TheAllowlist(unittest.TestCase):
    def test_empty_allows_anything(self):
        self.assertEqual(delivery.refusal(METADATA, delivery.allowlist("")), "")

    def test_a_listed_host_is_allowed_in_any_case(self):
        allowed = delivery.allowlist("Partner.Example, 127.0.0.1")
        self.assertEqual(delivery.refusal("https://partner.example:4080/as2", allowed), "")
        self.assertEqual(delivery.refusal("http://127.0.0.1:9/as2", allowed), "")

    def test_anything_else_is_refused_and_named(self):
        reason = delivery.refusal(METADATA, delivery.allowlist("partner.example"))
        self.assertIn("169.254.169.254", reason)
        self.assertIn("--deliver-to", reason)


class Receiver(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Receiver.received.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class ACourierWithAnAllowlist(MockServerCase):
    config_kwargs = {"deliver_to": "127.0.0.1"}

    def setUp(self):
        super().setUp()
        Receiver.received = []
        self.listener = HTTPServer(("127.0.0.1", 0), Receiver)
        threading.Thread(target=self.listener.serve_forever, daemon=True).start()
        self.addCleanup(self.listener.server_close)
        self.addCleanup(self.listener.shutdown)
        self.port = self.listener.server_address[1]

    def test_a_partner_url_outside_it_is_refused_by_the_control_plane(self):
        status, _h, data = self.patch("/_mock/partners/" + ACME, {"as2_url": METADATA})
        self.assertEqual(status, 400, data)
        self.assertIn("169.254.169.254", data["error"])
        status, _h, data = self.post("/_mock/partners", {
            "id": "SNOOP", "name": "Snoop", "as2_url": METADATA})
        self.assertEqual(status, 400, data)

    def test_a_listed_host_is_posted_to(self):
        url = "http://127.0.0.1:%d/as2" % self.port
        status, _h, data = self.patch("/_mock/partners/" + ACME, {"as2_url": url})
        self.assertEqual(status, 200, data)
        self.send(x12_order("PO-ALLOWED"))
        self.httpd.mock.courier.drain(20.0)
        self.assertEqual(len(Receiver.received), 4)

    def test_a_url_that_got_in_another_way_is_still_not_posted_to(self):
        # A row from an older file, or from before --deliver-to was set.
        with self.httpd.mock.lock:
            self.httpd.mock.conn.execute(
                "UPDATE partner SET as2_url = ? WHERE id = ?", (METADATA, ACME))
            self.httpd.mock.conn.commit()
        self.send(x12_order("PO-REFUSED"))
        self.httpd.mock.courier.drain(20.0)
        _s, _h, rows = self.get("/_mock/outbox")
        self.assertTrue(rows)
        self.assertTrue(all(row["status"] == "failed" for row in rows), rows)
        _s, _h, state = self.get("/_mock/state")
        self.assertTrue(any("--deliver-to" in f for f in state["courierFailures"]))

    def test_an_asynchronous_mdn_outside_it_is_answered_now_with_a_failure(self):
        status, headers, body = self.request(
            "POST", "/as2", x12_order("PO-ASYNC-REFUSED"),
            headers=as2_headers(async_url=METADATA), raw=True)
        self.assertEqual(status, 200)
        self.assertIn("multipart/report", headers["Content-Type"])
        text = body.decode()
        self.assertIn("failed", text)
        self.assertIn("--deliver-to", text)
        # Not read, as any refused receipt: the sender has to send it again.
        status, _h, _data = self.get("/_mock/orders/PO-ASYNC-REFUSED")
        self.assertEqual(status, 404)
        self.assertEqual(Receiver.received, [])

    def test_an_asynchronous_mdn_to_a_listed_host_is_posted(self):
        url = "http://127.0.0.1:%d/mdn" % self.port
        status, _h, _body = self.request(
            "POST", "/as2", x12_order("PO-ASYNC-OK"),
            headers=as2_headers(async_url=url), raw=True)
        self.assertEqual(status, 202)
        self.httpd.mock.courier.drain(20.0)
        self.assertEqual(Receiver.received, ["/mdn"])


class AuthenticationThatIsNotAscii(MockServerCase):
    """The credentials are compared as bytes, in constant time."""
    config_kwargs = {"basic_auth": "edi:sécret"}

    def setUp(self):
        pass        # reset itself needs credentials

    def get_with(self, credentials):
        import base64
        token = base64.b64encode(credentials.encode("utf-8")).decode()
        return self.get("/_mock/health", headers={"Authorization": "Basic " + token})

    def test_the_right_ones_work(self):
        status, _h, _data = self.get_with("edi:sécret")
        self.assertEqual(status, 200)

    def test_a_near_miss_does_not(self):
        status, _h, _data = self.get_with("edi:secret")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
