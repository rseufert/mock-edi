"""A mock left running stays bounded: --keep-requests and --retention-days (#48)."""
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import db, server

from support import ACME, FileDatabaseCase, MockServerCase, x12_order

LONG_AGO = "2000-01-01T00:00:00"


class KeepingTheNewestRequests(MockServerCase):
    config_kwargs = {"keep_requests": 5}

    def test_an_advance_trims_the_request_log(self):
        for _ in range(10):
            self.get("/_mock/health")
        self.post("/_mock/advance")
        _s, _h, rows = self.get("/_mock/requests?limit=1000")
        # The five kept, plus the requests logged since the prune: the
        # advance itself and this read.
        self.assertLessEqual(len(rows), 7)
        self.assertEqual(rows[-1]["path"], "/_mock/health")
        _s, _h, state = self.get("/_mock/state")
        self.assertGreaterEqual(state["retention"]["pruned"]["request_log"], 5)

    def test_it_is_pruned_every_so_many_requests_without_an_advance(self):
        original = server.PRUNE_EVERY
        server.PRUNE_EVERY = 3
        self.addCleanup(setattr, server, "PRUNE_EVERY", original)
        for _ in range(12):
            self.get("/_mock/health")
        _s, _h, rows = self.get("/_mock/requests?limit=1000")
        self.assertLessEqual(len(rows), 5 + 3)


class TheDefault(MockServerCase):
    def test_a_test_partner_keeps_what_it_did(self):
        self.send(x12_order("PO-DEFAULT"))
        self.post("/_mock/advance")
        _s, _h, state = self.get("/_mock/state")
        self.assertEqual(state["retention"]["keepRequests"], 5000)
        self.assertEqual(state["retention"]["retentionDays"], 0)
        self.assertEqual(state["retention"]["pruned"], {})
        self.assertEqual(len(self.order("PO-DEFAULT")["shipments"]), 1)


class RetentionDays(MockServerCase):
    config_kwargs = {"retention_days": 30, "invoice_delay_ms": 3600000}

    def backdate(self):
        """Make everything so far look a very long time old."""
        conn = self.httpd.mock.conn
        with self.httpd.mock.lock:
            for table in ("request_log", "interchange", "outbound", "mdn"):
                conn.execute("UPDATE %s SET at = ?" % table, (LONG_AGO,))
            conn.commit()

    def count(self, table, where="1"):
        conn = self.httpd.mock.conn
        with self.httpd.mock.lock:
            return conn.execute(
                "SELECT COUNT(*) FROM %s WHERE %s" % (table, where)).fetchone()[0]

    def test_old_records_go_and_the_business_stays(self):
        self.send(x12_order("PO-OLD"))
        self.mailbox(ACME, leave=False)     # collected, so finished with
        self.backdate()
        self.post("/_mock/advance")
        self.assertEqual(self.count("interchange", "at = '%s'" % LONG_AGO), 0)
        self.assertEqual(self.count("outbound", "at = '%s'" % LONG_AGO), 0)
        self.assertEqual(self.count("transaction_set", "reference = 'PO-OLD'"), 0)
        # The order itself, and the partner, are what the mock is.
        self.assertEqual(self.order("PO-OLD")["po_number"], "PO-OLD")

    def test_nothing_still_waiting_is_removed(self):
        self.send(x12_order("PO-WAITING"))  # nothing collected: all ready
        self.backdate()
        self.post("/_mock/advance")
        self.assertEqual([r["code"] for r in self.mailbox(ACME)],
                         ["997", "855", "856"])

    def test_recent_records_are_kept(self):
        self.send(x12_order("PO-NEW"))
        self.post("/_mock/advance")
        self.assertGreater(self.count("interchange"), 0)
        _s, _h, state = self.get("/_mock/state")
        self.assertNotIn("interchange", state["retention"]["pruned"])


class PrunedAtStartup(FileDatabaseCase):
    config_kwargs = {"retention_days": 30}

    def test_a_restart_prunes_what_has_aged(self):
        self.send(x12_order("PO-AGED"))
        self.mailbox(ACME, leave=False)
        self.stop()
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE interchange SET at = ?", (LONG_AGO,))
        conn.commit()
        conn.close()
        self.start()
        _s, _h, state = self.get("/_mock/state")
        self.assertGreater(state["retention"]["pruned"]["interchange"], 0)
        _s, _h, rows = self.get("/_mock/interchanges")
        self.assertEqual(rows, [])


class TheLookupsHaveIndexes(unittest.TestCase):
    def plan(self, conn, sql):
        return " ".join(str(row[-1]) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + sql, ("PO-1",)))

    def test_shipments_and_invoices_are_found_by_po_number_without_a_scan(self):
        conn = db.connect(":memory:")
        try:
            for table, index in (("shipment", "ix_shipment_po"),
                                 ("invoice", "ix_invoice_po")):
                plan = self.plan(conn, "SELECT * FROM %s WHERE po_number = ?" % table)
                self.assertIn(index, plan)
        finally:
            conn.close()


class PruneItself(unittest.TestCase):
    def test_zero_keeps_everything(self):
        conn = db.connect(":memory:")
        try:
            for _ in range(3):
                conn.execute("INSERT INTO request_log (method, path, status, at)"
                             " VALUES ('GET', '/', 200, ?)", (LONG_AGO,))
            self.assertEqual(db.prune(conn, 0, 0), {})
            self.assertEqual(db.prune(conn, 2, 0), {"request_log": 1})
            self.assertEqual(db.prune(conn, 0, 1), {"request_log": 2})
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
