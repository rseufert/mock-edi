"""A --db file from an earlier version: opened and upgraded in place, or refused.

`--db` exists so that state and control numbers survive a restart, which
means they have to survive an upgrade too. The first user to upgrade a
long-running mock used to meet `no such column: ack_status` and a process that
never bound its port.
"""
import hashlib
import json
import os
import sqlite3
import sys
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import db
from mockedi.server import Config, make_server

from support import REQUEST_TIMEOUT, FileDatabaseCase, x12_order

OLD_SCHEMA = os.path.join(HERE, "fixtures", "schema-0.1.0.sql")


class FileDatabase(FileDatabaseCase):
    start_on_setup = False

    def serve(self):
        httpd = self.start()
        return httpd, self.base

    def user_version(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()


class From010(FileDatabase):
    """A file with 0.1.0's schema and one of 0.1.0's orders already in it."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        with open(OLD_SCHEMA, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO transaction_set (interchange_id, direction, dialect,"
            " partner, code, kind, control, reference, at)"
            " VALUES (1, 'out', 'X12', 'ACME', '855', 'response', '0001',"
            " 'PO-FROM-010', '2026-09-24T10:30:00')")
        conn.commit()
        conn.close()

    def test_it_opens_and_takes_an_order(self):
        _httpd, base = self.serve()
        request = urllib.request.Request(
            base + "/edi", data=x12_order("PO-AFTER-UPGRADE").encode(),
            method="POST")
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            summary = json.loads(response.read())
        self.assertEqual(summary["orders"], ["PO-AFTER-UPGRADE"])
        self.assertEqual([q["code"] for q in summary["queued"]],
                         ["997", "855", "856", "810"])

    def test_what_it_held_is_kept_and_new_columns_take_their_defaults(self):
        httpd, _base = self.serve()
        with httpd.mock.lock:
            row = httpd.mock.conn.execute(
                "SELECT reference, ack_status FROM transaction_set"
                " WHERE reference = 'PO-FROM-010'").fetchone()
        self.assertEqual(tuple(row), ("PO-FROM-010", ""))

    def test_it_is_marked_with_the_current_version(self):
        self.serve()
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)

    def test_the_upgrade_says_what_it_added_and_is_done_once(self):
        conn = sqlite3.connect(self.db_path)
        try:
            added = db.upgrade(conn, self.db_path)
            self.assertIn("transaction_set.ack_status", added)
            self.assertIn("outbound.set_control", added)
            self.assertEqual(db.upgrade(conn, self.db_path), [])
        finally:
            conn.close()


class FromANewerMock(FileDatabase):
    def test_it_is_refused_with_the_file_and_both_versions(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = %d" % (db.SCHEMA_VERSION + 1))
        conn.commit()
        conn.close()
        with self.assertRaises(db.DatabaseError) as caught:
            make_server(Config(host="127.0.0.1", port=0, db_path=self.db_path,
                               quiet=True))
        message = str(caught.exception)
        self.assertIn(self.db_path, message)
        self.assertIn("version %d" % (db.SCHEMA_VERSION + 1), message)
        self.assertIn("knows %d" % db.SCHEMA_VERSION, message)
        # And nothing was changed on the way to refusing it.
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION + 1)


class TheVersionMovesWithTheSchema(unittest.TestCase):
    # The schema as of SCHEMA_VERSION 5. When this fails, the schema has
    # changed: bump db.SCHEMA_VERSION, then record the new pair here. A file
    # written by the new schema must not look, to an older mock, like one it
    # understands.
    FINGERPRINT = (5, "51151a3bdb4ff74c")

    def test_a_changed_schema_has_a_new_version(self):
        text = " ".join((db.SCHEMA + db.INDEXES).split())
        found = (db.SCHEMA_VERSION, hashlib.sha256(text.encode()).hexdigest()[:16])
        self.assertEqual(found, self.FINGERPRINT,
                         "the schema changed: bump SCHEMA_VERSION and update "
                         "FINGERPRINT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
