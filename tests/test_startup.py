"""Starting up: nothing waits on a name server, and nothing is left in a buffer.

Two faults that only show themselves where the mock is actually deployed -
in a container, behind a supervisor, on a CI runner - and not on the laptop
where it was written. Both were found in mock-bank, which ported this code
(#136).

The standard library's `HTTPServer.server_bind` reverse-resolves the address
it just bound, to fill in a `server_name` that nothing here reads. Bound to
`0.0.0.0` with a resolver that does not answer, that is a minute of silence
before the port opens, and every waiter the project has - the Dockerfile's
health check, the CI smoke job, `mockedi.testing.Mock.start` - gives up long
before then and reports a start that failed for no visible reason.

The exposure warning is written to stderr, which is block-buffered when it is
piped rather than a terminal on every Python before 3.9. The mock then serves
forever, so the buffer is never filled and never flushed, and the one message
that says *anyone who can reach this port can reset your database* is still
sitting in memory when it matters. The test below does not need a 3.8 to show
this: it hands the child a deliberately block-buffered stderr, which is the
same condition, so the guard holds on every version CI runs.
"""
import io
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from mockedi import server
from mockedi.__main__ import line_buffer


class StartingUpDoesNotWaitForAResolver(unittest.TestCase):
    """The done-when: startup time does not depend on DNS."""

    def setUp(self):
        self.real_getfqdn = socket.getfqdn
        self.addCleanup(setattr, socket, "getfqdn", self.real_getfqdn)

    def serve(self, host="127.0.0.1"):
        httpd = server.make_server(
            server.Config(host=host, port=0, db_path=":memory:"))
        self.addCleanup(httpd.server_close)
        return httpd

    def test_the_bound_address_is_never_looked_up(self):
        looked_up = []

        def getfqdn(name=""):
            looked_up.append(name)
            return self.real_getfqdn(name)

        socket.getfqdn = getfqdn
        self.serve()
        self.assertEqual(looked_up, [])

    def test_so_a_resolver_that_does_not_answer_cannot_delay_the_bind(self):
        # Three seconds stands in for the minute mock-bank saw; the assertion
        # is that none of it is spent, not that some of it is.
        socket.getfqdn = lambda name="": time.sleep(3) or self.real_getfqdn(name)
        start = time.monotonic()
        self.serve("0.0.0.0")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_and_the_server_still_knows_where_it_is_listening(self):
        # server_name and server_port are what the override replaces the
        # lookup with; CGIHTTPRequestHandler and anything else built on
        # HTTPServer expects them to be set.
        httpd = self.serve()
        self.assertEqual(httpd.server_name, "127.0.0.1")
        self.assertEqual(httpd.server_port, httpd.socket.getsockname()[1])
        self.assertNotEqual(httpd.server_port, 0)


class AStartupMessageReachesAPipedStderr(unittest.TestCase):
    """The done-when: every startup message arrives immediately on 3.8."""

    def test_a_block_buffered_stream_is_line_buffered_in_place(self):
        stream = io.TextIOWrapper(io.BufferedWriter(io.BytesIO()),
                                  line_buffering=False)
        self.assertFalse(stream.line_buffering)
        line_buffer(stream)
        self.assertTrue(stream.line_buffering)

    def test_and_a_stream_that_cannot_be_is_left_alone(self):
        # Captured output under a test runner is often a plain StringIO with
        # no reconfigure at all. Startup must not die on it.
        plain = io.StringIO()
        line_buffer(plain)
        plain.write("still usable")
        self.assertEqual(plain.getvalue(), "still usable")

    def test_the_exposure_warning_arrives_while_the_server_is_running(self):
        # sys.stderr is replaced with a block-buffered wrapper over fd 2
        # before main() runs: exactly how a piped stderr behaves before 3.9,
        # on whichever Python is running this.
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import io, sys;"
             "sys.stderr = io.TextIOWrapper(open(2, 'wb', buffering=65536),"
             " line_buffering=False, write_through=False);"
             "import mockedi.__main__ as m;"
             "sys.exit(m.main())",
             "--host", "0.0.0.0", "--port", "0", "--db", ":memory:", "-q"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=dict(os.environ, PYTHONPATH=ROOT))
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.addCleanup(child.stderr.close)

        line = []
        reader = threading.Thread(target=lambda: line.append(
            child.stderr.readline()), daemon=True)
        reader.start()
        reader.join(15)

        self.assertIsNone(child.poll(),
                          "the server exited instead of serving")
        self.assertTrue(line, "no startup warning arrived within 15s")
        self.assertIn(b"WARNING", line[0])
        self.assertIn(b"no --auth", line[0])


if __name__ == "__main__":
    unittest.main()
