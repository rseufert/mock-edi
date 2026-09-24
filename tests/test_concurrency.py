"""Several clients at once.

A mock is asked to stand in for a trading partner, and trading partners get
hit by more than one thread.  This is the one test in the suite that is about
timing rather than shapes, so it is deliberately small and deliberately
specific: it drives real writes and real control-plane reads at the same time
and asserts that nothing answers 5xx.

It exists because of a real bug.  The request log was the single database
write not taken under the mock's lock, on the grounds that logging is
harmless - but `commit()` commits the *connection*, not the statement, so a
log entry written while another thread was mid-transaction committed that
thread's work early and left its own commit with nothing to do.  sqlite3
answers that with "cannot commit - no transaction is active", and the request
that had done the real work failed with a 500.  Python 3.9 on CI found it;
3.12 and later hide it, because their sqlite3 no longer raises.
"""
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import MockServerCase, x12_order

THREADS = 6
ROUNDS = 4


class SeveralClientsAtOnce(MockServerCase):
    def _hammer(self, index, failures):
        for round_ in range(ROUNDS):
            payload = x12_order("PO-%d-%d" % (index, round_)).encode()
            for url, data in ((self.base + "/edi", payload),
                              (self.base + "/_mock/state", None)):
                request = urllib.request.Request(
                    url, data=data, method="POST" if data else "GET")
                try:
                    with urllib.request.urlopen(request) as response:
                        response.read()
                except urllib.error.HTTPError as error:
                    with error:
                        body = error.read().decode("utf-8", "replace")[:200]
                    failures.append("%s %s -> %d: %s"
                                    % (request.get_method(), url, error.code, body))
                except Exception as error:      # pragma: no cover - socket trouble
                    failures.append("%s %s -> %s" % (request.get_method(), url, error))

    def test_concurrent_writes_and_reads_do_not_trip_over_each_other(self):
        failures = []
        threads = [threading.Thread(target=self._hammer, args=(index, failures))
                   for index in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(failures, [], "\n".join(failures[:5]))

    def test_every_order_survived(self):
        failures = []
        self._hammer(99, failures)
        self.assertEqual(failures, [])
        _status, _headers, rows = self.get("/_mock/orders?limit=100")
        numbers = {row["po_number"] for row in rows}
        for round_ in range(ROUNDS):
            self.assertIn("PO-99-%d" % round_, numbers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
