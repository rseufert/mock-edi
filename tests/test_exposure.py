"""What the mock says, and refuses, when it is reachable from a network.

Three things that are fine on a laptop and worth stating before the container
runs on a shared network: the control plane is unauthenticated by default and
can reset the database and read every archived document; the courier posts to
whatever URL it is given, including one an *unauthenticated* AS2 sender named
in `Receipt-Delivery-Option`; and the credential comparison was `==`.

None of this makes the mock a security product. It makes it say out loud what
it does, which is the difference between a tool you can reason about and one
you cannot.
"""
import base64
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import as2, delivery
from mockedi.__main__ import (build_parser, config_from_args, exposure_warning,
                              is_loopback)

from support import ACME, MockServerCase, as2_headers, x12_order


def config(*argv):
    return config_from_args(build_parser().parse_args(list(argv)))


class WhichAddressesAreReachable(unittest.TestCase):
    def test_loopback_is_recognised_in_its_several_spellings(self):
        for host in ("127.0.0.1", "127.0.0.53", "localhost", "::1", "[::1]"):
            self.assertTrue(is_loopback(host), host)

    def test_and_everything_else_is_not(self):
        for host in ("0.0.0.0", "", "::", "192.168.1.5", "10.0.0.2",
                     "mock.internal"):
            self.assertFalse(is_loopback(host), host)


class TheStartupWarning(unittest.TestCase):
    def test_a_reachable_mock_with_no_auth_is_warned_about(self):
        warning = exposure_warning(config("--host", "0.0.0.0"))
        self.assertIn("--auth", warning)
        self.assertIn("0.0.0.0", warning)

    def test_it_names_what_is_at_stake(self):
        warning = exposure_warning(config("--host", "0.0.0.0"))
        self.assertIn("/_mock/reset", warning)

    def test_auth_silences_it(self):
        self.assertEqual(exposure_warning(config("--host", "0.0.0.0",
                                                 "--auth", "u:p")), "")

    def test_loopback_silences_it(self):
        self.assertEqual(exposure_warning(config("--host", "127.0.0.1")), "")

    def test_the_default_is_silent(self):
        self.assertEqual(exposure_warning(config()), "")


class WhereTheCourierMayPost(unittest.TestCase):
    def test_no_allowlist_permits_anywhere(self):
        self.assertTrue(delivery.permitted("http://anywhere.test/as2", ()))

    def test_a_host_on_the_list_is_permitted(self):
        self.assertTrue(delivery.permitted("http://partner.test/as2",
                                           ("partner.test",)))

    def test_the_port_is_not_part_of_the_comparison(self):
        self.assertTrue(delivery.permitted("http://partner.test:9000/as2",
                                           ("partner.test",)))

    def test_case_does_not_matter(self):
        self.assertTrue(delivery.permitted("http://PARTNER.test/as2",
                                           ("partner.TEST",)))

    def test_a_host_off_the_list_is_refused(self):
        # The case worth having: a metadata endpoint, or a neighbour.
        self.assertFalse(delivery.permitted("http://169.254.169.254/latest/meta-data",
                                            ("partner.test",)))

    def test_a_url_with_no_host_is_refused_rather_than_allowed(self):
        for url in ("", "not a url", "file:///etc/passwd"):
            self.assertFalse(delivery.permitted(url, ("partner.test",)), url)

    def test_the_flag_is_read_as_a_list(self):
        self.assertEqual(config("--deliver-to", "a.test, b.test").deliver_to,
                         ("a.test", "b.test"))

    def test_and_is_empty_by_default(self):
        self.assertEqual(config().deliver_to, ())


class Listener(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        Listener.received.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class ARefusedDeliveryTarget(MockServerCase):
    """With --deliver-to set, the courier posts only where it is allowed."""

    config_kwargs = {"deliver_to": ("allowed.test",), "deliver_timeout": 3.0}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.listener = HTTPServer(("127.0.0.1", 0), Listener)
        cls.port = cls.listener.server_address[1]
        cls.thread = threading.Thread(target=cls.listener.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        Listener.received = []

    def test_a_partner_url_off_the_list_is_not_posted_to(self):
        self.patch("/_mock/partners/" + ACME,
                   {"as2_url": "http://127.0.0.1:%d/as2" % self.port})
        self.send(x12_order("EXPOSED-A"))
        self.settle()
        self.assertEqual(Listener.received, [])

    def test_and_the_outbox_says_why(self):
        self.patch("/_mock/partners/" + ACME,
                   {"as2_url": "http://127.0.0.1:%d/as2" % self.port})
        self.send(x12_order("EXPOSED-B"))
        self.settle()
        _status, _headers, rows = self.get("/_mock/outbox")
        refused = [row for row in rows if row["status"] == "failed"]
        self.assertTrue(refused)
        self.assertIn("--deliver-to", refused[0]["note"])

    def test_an_async_mdn_to_a_refused_url_is_answered_not_posted(self):
        url = "http://127.0.0.1:%d/mdn" % self.port
        status, _headers, body = self.request(
            "POST", "/as2", x12_order("EXPOSED-C"),
            headers=as2_headers(async_url=url), raw=True)
        # Answered here and now, rather than 202 and a post to their address.
        self.assertEqual(status, 200)
        text = body.decode("utf-8", "replace")
        self.assertIn("failed/Failure", text)
        self.assertIn("--deliver-to", text)
        self.httpd.mock.courier.drain(5.0)
        self.assertEqual(Listener.received, [])

    def test_the_interchange_behind_a_refused_receipt_is_not_acted_on(self):
        url = "http://127.0.0.1:%d/mdn" % self.port
        self.request("POST", "/as2", x12_order("EXPOSED-D"),
                     headers=as2_headers(async_url=url), raw=True)
        status, _headers, _data = self.get("/_mock/orders/EXPOSED-D")
        self.assertEqual(status, 404)


class TheCredentialComparison(MockServerCase):
    config_kwargs = {"basic_auth": "edi:s3cret"}

    def header(self, credential):
        return {"Authorization": "Basic " + base64.b64encode(
            credential.encode()).decode()}

    def test_the_right_credential_is_accepted(self):
        status, _headers, _data = self.get(
            "/_mock/health", headers=self.header("edi:s3cret"))
        self.assertEqual(status, 200)

    def test_a_wrong_one_is_not(self):
        status, _headers, _data = self.get(
            "/_mock/health", headers=self.header("edi:wrong"))
        self.assertEqual(status, 401)

    def test_a_prefix_of_the_right_one_is_not(self):
        status, _headers, _data = self.get(
            "/_mock/health", headers=self.header("edi:s3cre"))
        self.assertEqual(status, 401)

    def test_it_is_compared_in_constant_time(self):
        import inspect

        from mockedi import server
        source = inspect.getsource(server.Handler._authorised)
        self.assertIn("compare_digest", source)
        self.assertNotIn("decoded == ", source)


if __name__ == "__main__":
    unittest.main()
