"""The command-line flags, each shown to do what `--help` says it does.

Every other module configures the mock through `Config` directly. These go
through the flags a user types - `mockedi/__main__.py` is the one module the
rest of the suite never imports, and the one whose drift a user meets first.
"""
import dataclasses
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi.__main__ import build_parser, config_from_args
from mockedi.server import Config

from support import MockServerCase, as2_headers, x12_order


class TheCommandLine(unittest.TestCase):
    """argparse and Config agree: every flag lands on the field it names."""

    def config(self, *argv):
        return config_from_args(build_parser().parse_args(list(argv)))

    def test_no_flags_is_the_default_config(self):
        self.assertEqual(self.config(), Config())

    def test_every_destination_is_a_config_field(self):
        fields = {f.name for f in dataclasses.fields(Config)}
        for action in build_parser()._actions:
            if action.dest in ("help", "version"):
                continue
            self.assertIn(action.dest, fields,
                          "%s has nowhere to go" % "/".join(action.option_strings))

    def test_each_flag_reaches_its_field(self):
        config = self.config(
            "--host", "0.0.0.0", "--port", "9", "--db", "x.db",
            "--as2-id", "US", "--name", "Us Ltd", "--qualifier", "01",
            "--ack-delay", "1", "--response-delay", "2", "--despatch-delay", "3",
            "--invoice-delay", "4", "--tax-rate", "0.1", "--allow-duplicates",
            "--no-mdn", "--any-receiver", "--compact",
            "--drop-dir", "in", "--pickup-dir", "out",
            "--drop-interval-ms", "5", "--drop-settle-ms", "6",
            "--auth", "u:p", "--seed", "7", "--latency-ms", "8",
            "--error-rate", "0.5", "--no-request-log", "-q")
        self.assertEqual(config, Config(
            host="0.0.0.0", port=9, db_path="x.db",
            as2_id="US", name="Us Ltd", qualifier="01",
            ack_delay_ms=1, response_delay_ms=2, despatch_delay_ms=3,
            invoice_delay_ms=4, tax_rate="0.1", allow_duplicates=True,
            mdn=False, strict_receiver=False, pretty=False,
            drop_dir="in", pickup_dir="out",
            drop_interval_ms=5, drop_settle_ms=6,
            basic_auth="u:p", seed_value=7, latency_ms=8,
            error_rate=0.5, log_requests=False, quiet=True))


class AddressedToSomeoneElse(MockServerCase):
    def test_it_is_refused_by_default(self):
        status, _h, data = self.post(
            "/edi", x12_order("PO-ELSEWHERE", receiver="REALPARTNER"),
            headers={"Content-Type": "application/edi-x12"})
        self.assertEqual(status, 422, data)
        self.assertIn("addressed to 'REALPARTNER'", data["error"])


class AnyReceiver(MockServerCase):
    config_kwargs = {"strict_receiver": False}          # --any-receiver

    def test_a_misaddressed_interchange_is_accepted(self):
        data = self.send(x12_order("PO-ELSEWHERE", receiver="REALPARTNER"))
        self.assertEqual(data["orders"], ["PO-ELSEWHERE"])


class NoMdn(MockServerCase):
    config_kwargs = {"mdn": False}                      # --no-mdn

    def test_a_sender_that_asks_for_one_gets_none(self):
        status, headers, body = self.request(
            "POST", "/as2", x12_order("PO-NO-MDN"), headers=as2_headers(),
            raw=True)
        self.assertEqual(status, 200)
        self.assertNotIn("multipart/report", headers["Content-Type"])
        _s, _h, rows = self.get("/_mock/mdns")
        self.assertEqual(rows, [])
        self.assertEqual(self.order("PO-NO-MDN")["status"], "invoiced")


class ErrorRate(MockServerCase):
    config_kwargs = {"error_rate": 1.0}                 # --error-rate 1

    def test_a_trading_request_fails(self):
        status, _h, body = self.post(
            "/edi", x12_order("PO-FAULT"),
            headers={"Content-Type": "application/edi-x12"}, raw=True)
        self.assertEqual(status, 500)
        self.assertIn(b"injected failure", body)

    def test_the_control_plane_never_does(self):
        status, _h, _data = self.get("/_mock/health")
        self.assertEqual(status, 200)


class Latency(MockServerCase):
    config_kwargs = {"latency_ms": 300}                 # --latency-ms 300

    def test_every_request_is_held_back(self):
        started = time.monotonic()
        self.get("/_mock/health")
        self.assertGreaterEqual(time.monotonic() - started, 0.3)


class NoRequestLog(MockServerCase):
    config_kwargs = {"log_requests": False}             # --no-request-log

    def test_the_table_stays_empty(self):
        self.send(x12_order("PO-UNLOGGED"))
        self.get("/_mock/health")
        _s, _h, rows = self.get("/_mock/requests")
        self.assertEqual(rows, [])


class Refuser(BaseHTTPRequestHandler):
    """A partner whose MDN endpoint answers every POST with a 500."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class AnAsynchronousMdnTheSenderRefuses(MockServerCase):
    def setUp(self):
        super().setUp()
        self.listener = HTTPServer(("127.0.0.1", 0), Refuser)
        threading.Thread(target=self.listener.serve_forever, daemon=True).start()
        self.addCleanup(self.listener.server_close)
        self.addCleanup(self.listener.shutdown)
        url = "http://127.0.0.1:%d/mdn" % self.listener.server_address[1]
        status, _h, _body = self.request(
            "POST", "/as2", x12_order("PO-MDN-REFUSED"),
            headers=as2_headers(async_url=url), raw=True)
        self.assertEqual(status, 202)
        self.httpd.mock.courier.drain(20.0)

    def test_it_is_recorded_as_failed(self):
        _s, _h, rows = self.get("/_mock/mdns")
        self.assertEqual(rows[0]["status"], "failed")

    def test_the_failure_is_reported(self):
        _s, _h, state = self.get("/_mock/state")
        self.assertTrue(any(f.startswith("mdn ") and "HTTP 500" in f
                            for f in state["courierFailures"]),
                        state["courierFailures"])

    def test_a_reset_forgets_it(self):
        # What the courier remembers describes data a reset throws away (#32).
        self.post("/_mock/reset")
        _s, _h, state = self.get("/_mock/state")
        self.assertEqual(state["courierFailures"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
