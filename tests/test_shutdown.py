"""Closing the mock while its background threads are still at work.

SQLite closed under a running statement is a use-after-free in C: the
interpreter crashes with a segmentation fault instead of raising, which is how
this was found - CI exiting 139 after the last test had passed.
"""
import contextlib
import io
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi.server import Config, make_server

from support import ACME, x12_order


def serve(**config):
    httpd = make_server(Config(host="127.0.0.1", port=0, db_path=":memory:",
                               quiet=True, **config))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


def call(base, method, path, body):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    with urllib.request.urlopen(request) as response:
        return response.status


class ClosingUnderTheLock(unittest.TestCase):
    def test_close_waits_for_a_thread_holding_the_lock(self):
        """A thread between two queries of one piece of work keeps the connection.

        The crash itself cannot be provoked on demand: SQLite's own mutex holds
        close() off while a statement runs, and the window that remains - after
        Python checks the connection is open, before the call enters SQLite -
        is too narrow to hit on purpose. What can be tested is the guarantee
        that closes that window: nothing is closed while the lock is held.
        """
        httpd, _base = serve()
        mock = httpd.mock
        inside, times, answers = threading.Event(), {}, []

        def worker():
            # What the courier and the poller do: take the lock, then do
            # several things with the connection before letting go.
            with mock.lock:
                mock.conn.execute("SELECT 1").fetchone()
                inside.set()
                time.sleep(0.5)
                answers.append(mock.conn.execute("SELECT 2").fetchone()[0])
                times["done"] = time.monotonic()

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertTrue(inside.wait(5))
        httpd.shutdown()
        httpd.server_close()           # closes the mock, and its connection
        times["close"] = time.monotonic()
        thread.join(5)

        self.assertEqual(answers, [2])
        self.assertLessEqual(times["done"], times["close"])


class ACourierThatWillNotStop(unittest.TestCase):
    """A delivery in flight is a network call; nothing can interrupt it."""

    def setUp(self):
        # A partner endpoint that accepts the connection and never answers.
        self.endpoint = socket.socket()
        self.endpoint.bind(("127.0.0.1", 0))
        self.endpoint.listen(1)
        self.connected, self.release = threading.Event(), threading.Event()

        def hang():
            try:
                connection, _ = self.endpoint.accept()
            except OSError:            # closed by tearDown before anyone came
                return
            self.connected.set()
            self.release.wait(10)
            connection.close()

        self.hanger = threading.Thread(target=hang, daemon=True)
        self.hanger.start()

    def tearDown(self):
        self.release.set()
        self.endpoint.close()

    def test_it_is_named_and_meets_a_closed_database_not_a_crash(self):
        httpd, base = serve(deliver_timeout=10.0)
        url = "http://127.0.0.1:%d/as2" % self.endpoint.getsockname()[1]
        call(base, "PATCH", "/_mock/partners/" + ACME, {"as2_url": url})
        call(base, "POST", "/edi", x12_order("PO-HANG").encode())
        self.assertTrue(self.connected.wait(5), "the courier never connected")
        httpd.shutdown()

        courier = httpd.mock.courier
        worker = courier._thread
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            stuck = httpd.mock.close(wait=0.2)
        httpd.socket.close()

        self.assertEqual(stuck, ["courier"])
        self.assertIn("courier did not stop", said.getvalue())

        # Let the delivery finish: the courier takes the lock, finds the
        # connection closed, and records that as a failure.
        self.release.set()
        worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertTrue(any("closed" in failure for failure in courier.failures),
                        courier.failures)


class AnIdleMock(unittest.TestCase):
    def test_a_courier_with_nothing_to_do_stops_at_once(self):
        httpd, _base = serve()
        httpd.shutdown()
        self.assertEqual(httpd.mock.close(wait=0.2), [])
        httpd.socket.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
