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


ROLE_COLUMN = """,
    -- What the partner is to the mock: a `customer` it sells to, or a
    -- `supplier` it buys from. The mock's own side (schema.SELLER/BUYER) is
    -- derived from this in one place, partners.mock_role, never stored.
    role         TEXT NOT NULL DEFAULT 'customer'
);"""
# Version 9's, which a version 7 file does not have either.
DIRECTION_COLUMN = "\n    direction    TEXT NOT NULL DEFAULT 'received',"


# Before version 10 an order was keyed by its number alone (#132). Each change
# is made inside its own table's CREATE, since `shipment` has the same lines.
PARTNER_KEYS = {
    "purchase_order": (
        ("    po_number    TEXT NOT NULL,", "    po_number    TEXT PRIMARY KEY,"),
        (",\n    PRIMARY KEY (partner, po_number)\n", "\n")),
    "order_line": (
        ("    partner       TEXT NOT NULL,\n", ""),
        ("PRIMARY KEY (partner, po_number, line)", "PRIMARY KEY (po_number, line)")),
}
OLD_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_line_po ON order_line (po_number);
CREATE INDEX IF NOT EXISTS ix_shipment_po ON shipment (po_number);
CREATE INDEX IF NOT EXISTS ix_invoice_po ON invoice (po_number);
"""


def schema_before_10(schema=None):
    schema = db.SCHEMA if schema is None else schema
    for table, changes in PARTNER_KEYS.items():
        start = schema.index("CREATE TABLE IF NOT EXISTS %s (" % table)
        end = schema.index("\n);", start) + 3
        block = schema[start:end]
        for new, old in changes:
            assert new in block, "the order keys moved; rebuild the old schema here"
            block = block.replace(new, old)
        schema = schema[:start] + block + schema[end:]
    return schema


def indexes_before_10():
    return "\n".join(line for line in db.INDEXES.split("\n")
                     if "ix_order_number" not in line
                     and "_order ON" not in line) + OLD_INDEXES


class From7(FileDatabase):
    """A version 7 file: partners with no role, all of them customers."""

    def setUp(self):
        super().setUp()
        for column in (ROLE_COLUMN, DIRECTION_COLUMN):
            self.assertIn(column, db.SCHEMA,
                          "a column moved; rebuild the version 7 schema here")
        conn = sqlite3.connect(self.db_path)
        conn.executescript(schema_before_10(
            db.SCHEMA.replace(ROLE_COLUMN, "\n);").replace(DIRECTION_COLUMN, "")))
        conn.executescript(indexes_before_10())
        for pid, behaviour in (("ACME", "accept"), ("GLOBEX", "short-ship")):
            conn.execute("INSERT INTO partner (id, name, behaviour)"
                         " VALUES (?, ?, ?)", (pid, pid.title(), behaviour))
        conn.execute("PRAGMA user_version = 7")
        conn.commit()
        conn.close()

    def test_every_partner_it_held_is_a_customer(self):
        httpd, _base = self.serve()
        with httpd.mock.lock:
            rows = httpd.mock.conn.execute(
                "SELECT id, role, behaviour FROM partner ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [("ACME", "customer", "accept"),
                          ("GLOBEX", "customer", "short-ship")])

    def test_no_supplier_is_added_to_a_file_that_has_partners(self):
        # Seeding is for an empty database; a file keeps the partners it had.
        httpd, _base = self.serve()
        with httpd.mock.lock:
            count = httpd.mock.conn.execute(
                "SELECT COUNT(*) FROM partner WHERE role = 'supplier'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_the_upgrade_adds_the_role_and_marks_it_current(self):
        conn = sqlite3.connect(self.db_path)
        try:
            added = db.upgrade(conn, self.db_path)
        finally:
            conn.close()
        # Version 8 added the role, 9 an order's direction, and 10 the
        # partner an order line belongs to.
        self.assertEqual(sorted(added), ["order_line.partner", "partner.role",
                                         "purchase_order.direction"])
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)


class From9(FileDatabase):
    """A version 9 file: orders keyed by their number alone, two partners'."""

    def setUp(self):
        super().setUp()
        conn = sqlite3.connect(self.db_path)
        conn.executescript(schema_before_10())
        conn.executescript(indexes_before_10())
        for pid in ("ACME", "GLOBEX"):
            conn.execute("INSERT INTO partner (id, name) VALUES (?, ?)",
                         (pid, pid.title()))
        for po_number, partner, lines in (("PO-A", "ACME", ("1", "2")),
                                          ("PO-G", "GLOBEX", ("1",))):
            conn.execute("INSERT INTO purchase_order (po_number, partner, status,"
                         " total, direction, at) VALUES (?,?,?,?,?,?)",
                         (po_number, partner, "received", "10.00", "received",
                          "2026-09-27T00:00:00Z"))
            for line in lines:
                conn.execute("INSERT INTO order_line (po_number, line, sku,"
                             " quantity, confirmed) VALUES (?,?,?,?,?)",
                             (po_number, line, "WIDGET-001", "5", "5"))
        conn.execute("PRAGMA user_version = 9")
        conn.commit()
        conn.close()

    def upgrade(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return db.upgrade(conn, self.db_path)
        finally:
            conn.close()

    def rows(self, sql):
        conn = sqlite3.connect(self.db_path)
        try:
            return [tuple(row) for row in conn.execute(sql)]
        finally:
            conn.close()

    def test_every_order_and_line_is_carried_across_with_its_partner(self):
        self.assertEqual(self.upgrade(), ["order_line.partner"])
        self.assertEqual(self.rows("SELECT partner, po_number, total FROM"
                                   " purchase_order ORDER BY po_number"),
                         [("ACME", "PO-A", "10.00"), ("GLOBEX", "PO-G", "10.00")])
        self.assertEqual(self.rows("SELECT partner, po_number, line, confirmed FROM"
                                   " order_line ORDER BY po_number, line"),
                         [("ACME", "PO-A", "1", "5"), ("ACME", "PO-A", "2", "5"),
                          ("GLOBEX", "PO-G", "1", "5")])
        self.assertEqual(self.user_version(), db.SCHEMA_VERSION)

    def test_the_key_is_the_partner_and_the_number(self):
        self.upgrade()
        keys = {table: sorted((row[5], row[1]) for row in self.rows(
                    "PRAGMA table_info(%s)" % table) if row[5])
                for table in ("purchase_order", "order_line")}
        self.assertEqual(keys, {
            "purchase_order": [(1, "partner"), (2, "po_number")],
            "order_line": [(1, "partner"), (2, "po_number"), (3, "line")]})

    def test_the_indexes_on_the_number_alone_are_gone(self):
        self.upgrade()
        names = {row[0] for row in self.rows(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertFalse(names & {"ix_line_po", "ix_shipment_po", "ix_invoice_po"})
        self.assertTrue({"ix_order_number", "ix_shipment_order",
                         "ix_invoice_order"} <= names)

    def test_it_is_done_once(self):
        self.upgrade()
        self.assertEqual(self.upgrade(), [])

    def test_and_then_a_second_partner_can_use_the_same_number(self):
        httpd, _base = self.serve()
        self.post("/edi", x12_order("PO-A", sender="GLOBEX"),
                  headers={"Content-Type": "application/edi-x12"})
        self.assertEqual(sorted(self.rows("SELECT partner FROM purchase_order"
                                          " WHERE po_number = 'PO-A'")),
                         [("ACME",), ("GLOBEX",)])
        self.assertEqual(self.rows("SELECT line FROM order_line WHERE"
                                   " partner = 'ACME' AND po_number = 'PO-A'"),
                         [("1",), ("2",)])


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
    # The schema as of SCHEMA_VERSION 10. When this fails, the schema has
    # changed: bump db.SCHEMA_VERSION, then record the new pair here. A file
    # written by the new schema must not look, to an older mock, like one it
    # understands.
    FINGERPRINT = (10, "b6cd5617aee2e2bb")

    def test_a_changed_schema_has_a_new_version(self):
        text = " ".join((db.SCHEMA + db.INDEXES).split())
        found = (db.SCHEMA_VERSION, hashlib.sha256(text.encode()).hexdigest()[:16])
        self.assertEqual(found, self.FINGERPRINT,
                         "the schema changed: bump SCHEMA_VERSION and update "
                         "FINGERPRINT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
