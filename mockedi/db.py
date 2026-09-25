"""SQLite: the schema, the number ranges, and the demo data.

The mock is a *trading partner*, so its state is the state a trading partner
keeps: who it trades with, what it sells, every interchange that arrived or
left, and the documents those interchanges are about.

Three things here are worth knowing:

**Money is stored as text.**  Decimal strings, converted with `decimal.Decimal`
when arithmetic is needed.  Floating point would drift, and an invoice total
that is a cent out is exactly the bug an integration test exists to catch.

**Control numbers are ranges, not counters in a variable.**  ISA13, GS06 and
ST02 each advance per partner and survive a restart when the mock runs on a
file database - because duplicate control numbers are a real trading-partner
failure, and testing your handling of one means being able to replay it.

**The seed is deterministic.**  The same `--seed` gives the same catalogue,
the same partners and the same history, so a test may assert on prices.
"""
from __future__ import annotations

import datetime
import os
import random
import sqlite3
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS partner (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    qualifier    TEXT NOT NULL DEFAULT 'ZZ',
    dialect      TEXT NOT NULL DEFAULT 'X12',
    version      TEXT NOT NULL DEFAULT '004010',
    behaviour    TEXT NOT NULL DEFAULT 'accept',
    as2_url      TEXT NOT NULL DEFAULT '',
    mdn_mode     TEXT NOT NULL DEFAULT 'sync',
    street       TEXT NOT NULL DEFAULT '',
    city         TEXT NOT NULL DEFAULT '',
    region       TEXT NOT NULL DEFAULT '',
    postal       TEXT NOT NULL DEFAULT '',
    country      TEXT NOT NULL DEFAULT 'US',
    duns         TEXT NOT NULL DEFAULT '',
    test         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS catalog (
    sku          TEXT PRIMARY KEY,
    description  TEXT NOT NULL,
    upc          TEXT NOT NULL DEFAULT '',
    price        TEXT NOT NULL,
    uom          TEXT NOT NULL DEFAULT 'EA',
    in_stock     INTEGER NOT NULL DEFAULT 0,
    lead_days    INTEGER NOT NULL DEFAULT 3
);

CREATE TABLE IF NOT EXISTS interchange (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction    TEXT NOT NULL,
    dialect      TEXT NOT NULL,
    partner      TEXT NOT NULL DEFAULT '',
    control      TEXT NOT NULL DEFAULT '',
    transport    TEXT NOT NULL DEFAULT 'http',
    message_id   TEXT NOT NULL DEFAULT '',
    mic          TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL,
    at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction_set (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    interchange_id INTEGER NOT NULL,
    direction      TEXT NOT NULL,
    dialect        TEXT NOT NULL,
    partner        TEXT NOT NULL DEFAULT '',
    code           TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT '',
    control        TEXT NOT NULL DEFAULT '',
    group_control  TEXT NOT NULL DEFAULT '',
    reference      TEXT NOT NULL DEFAULT '',
    accepted       INTEGER NOT NULL DEFAULT 1,
    findings       TEXT NOT NULL DEFAULT '',
    -- Filled in when the other side acknowledges a document we sent.
    ack_status     TEXT NOT NULL DEFAULT '',
    ack_code       TEXT NOT NULL DEFAULT '',
    ack_note       TEXT NOT NULL DEFAULT '',
    ack_at         TEXT NOT NULL DEFAULT '',
    at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_order (
    po_number    TEXT PRIMARY KEY,
    partner      TEXT NOT NULL,
    seller_order TEXT NOT NULL DEFAULT '',
    ordered_on   TEXT NOT NULL DEFAULT '',
    requested_on TEXT NOT NULL DEFAULT '',
    currency     TEXT NOT NULL DEFAULT 'USD',
    status       TEXT NOT NULL DEFAULT 'received',
    total        TEXT NOT NULL DEFAULT '0.00',
    ship_to_name TEXT NOT NULL DEFAULT '',
    ship_to_id   TEXT NOT NULL DEFAULT '',
    ship_to_street TEXT NOT NULL DEFAULT '',
    ship_to_city TEXT NOT NULL DEFAULT '',
    ship_to_region TEXT NOT NULL DEFAULT '',
    ship_to_postal TEXT NOT NULL DEFAULT '',
    ship_to_country TEXT NOT NULL DEFAULT 'US',
    at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_line (
    po_number     TEXT NOT NULL,
    line          TEXT NOT NULL,
    sku           TEXT NOT NULL DEFAULT '',
    upc           TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    quantity      TEXT NOT NULL DEFAULT '0',
    uom           TEXT NOT NULL DEFAULT 'EA',
    price         TEXT NOT NULL DEFAULT '0.00',
    ordered_price TEXT NOT NULL DEFAULT '0.00',
    status        TEXT NOT NULL DEFAULT '',
    confirmed     TEXT NOT NULL DEFAULT '0',
    shipped       TEXT NOT NULL DEFAULT '0',
    invoiced      TEXT NOT NULL DEFAULT '0',
    reason        TEXT NOT NULL DEFAULT '',
    scheduled_on  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (po_number, line)
);

CREATE TABLE IF NOT EXISTS shipment (
    shipment_id  TEXT PRIMARY KEY,
    po_number    TEXT NOT NULL,
    partner      TEXT NOT NULL,
    shipped_on   TEXT NOT NULL DEFAULT '',
    carrier      TEXT NOT NULL DEFAULT '',
    scac         TEXT NOT NULL DEFAULT '',
    tracking     TEXT NOT NULL DEFAULT '',
    bol          TEXT NOT NULL DEFAULT '',
    cartons      INTEGER NOT NULL DEFAULT 0,
    weight       TEXT NOT NULL DEFAULT '0',
    at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice (
    invoice_number TEXT PRIMARY KEY,
    po_number      TEXT NOT NULL,
    partner        TEXT NOT NULL,
    shipment_id    TEXT NOT NULL DEFAULT '',
    invoiced_on    TEXT NOT NULL DEFAULT '',
    currency       TEXT NOT NULL DEFAULT 'USD',
    subtotal       TEXT NOT NULL DEFAULT '0.00',
    tax            TEXT NOT NULL DEFAULT '0.00',
    total          TEXT NOT NULL DEFAULT '0.00',
    terms_days     INTEGER NOT NULL DEFAULT 30,
    discount_pct   TEXT NOT NULL DEFAULT '0',
    discount_days  INTEGER NOT NULL DEFAULT 0,
    at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    partner      TEXT NOT NULL,
    dialect      TEXT NOT NULL,
    code         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    reference    TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL,
    message_id   TEXT NOT NULL DEFAULT '',
    -- Three control numbers, because a 997 needs all three to be matched:
    -- ISA13 identifies the interchange, GS06 the functional group, ST02 the
    -- transaction set inside it.
    control      TEXT NOT NULL DEFAULT '',
    group_control TEXT NOT NULL DEFAULT '',
    set_control  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    due_at       TEXT NOT NULL,
    released_at  TEXT NOT NULL DEFAULT '',
    delivered_at TEXT NOT NULL DEFAULT '',
    delivery     TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    at           TEXT NOT NULL
);

-- Work the seller has decided to do, but has not done yet. A despatch
-- delay must postpone the *packing*, not merely the posting: a shipment
-- created the instant the order arrives cannot reflect a change that arrives
-- a minute later, and refusing every change is not fidelity, it is an
-- artefact of doing the work too early.
CREATE TABLE IF NOT EXISTS scheduled (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    partner      TEXT NOT NULL,
    po_number    TEXT NOT NULL,
    kind         TEXT NOT NULL,
    due_at       TEXT NOT NULL,
    done_at      TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mdn (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    partner      TEXT NOT NULL,
    direction    TEXT NOT NULL DEFAULT 'out',
    original_id  TEXT NOT NULL DEFAULT '',
    message_id   TEXT NOT NULL DEFAULT '',
    disposition  TEXT NOT NULL DEFAULT '',
    mic          TEXT NOT NULL DEFAULT '',
    mode         TEXT NOT NULL DEFAULT 'sync',
    url          TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    payload      TEXT NOT NULL DEFAULT '',
    at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS control_number (
    partner      TEXT NOT NULL,
    scope        TEXT NOT NULL,
    value        INTEGER NOT NULL,
    PRIMARY KEY (partner, scope)
);

CREATE TABLE IF NOT EXISTS request_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    method       TEXT NOT NULL,
    path         TEXT NOT NULL,
    status       INTEGER NOT NULL,
    bytes_in     INTEGER NOT NULL DEFAULT 0,
    bytes_out    INTEGER NOT NULL DEFAULT 0,
    partner      TEXT NOT NULL DEFAULT '',
    at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ts_reference ON transaction_set (reference);
CREATE INDEX IF NOT EXISTS ix_ts_kind ON transaction_set (kind, direction);
CREATE INDEX IF NOT EXISTS ix_ts_ack ON transaction_set (direction, ack_status);
CREATE INDEX IF NOT EXISTS ix_outbound_status ON outbound (status, due_at);
CREATE INDEX IF NOT EXISTS ix_scheduled_due ON scheduled (done_at, due_at);
CREATE INDEX IF NOT EXISTS ix_line_po ON order_line (po_number);
"""

# Where each generated number starts.  Recognisable on sight, the way real
# document numbers are: a shipment is never mistaken for an invoice.
RANGE_START = {
    "interchange": 1,
    "group": 1,
    "transaction": 1,
    "seller_order": 5100000,
    "shipment": 8000000,
    "invoice": 9000000,
    "bol": 700000,
    "message": 1,
}


class UnitOfWork:
    """A connection whose commits can be held back until a unit of work ends.

    Almost every helper commits as it goes, which is right on its own and
    wrong inside an inbound interchange: a failure halfway through left the
    interchange recorded, an order's lines deleted and nothing put back.
    Inside `atomic()`, `commit()` waits; the block commits once at the end,
    or rolls everything back if anything raised. Everything else is the
    connection itself.

    It is one long-lived object rather than a wrapper swapped in for the
    duration, because the courier picks the connection up before it takes
    the lock, and must never be holding one whose commits do nothing.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._depth = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def commit(self) -> None:
        if not self._depth:
            self._conn.commit()

    def atomic(self) -> "UnitOfWork._Atomic":
        return UnitOfWork._Atomic(self)

    class _Atomic:
        def __init__(self, owner: "UnitOfWork"):
            self.owner = owner

        def __enter__(self) -> None:
            self.owner._depth += 1

        def __exit__(self, kind, _value, _traceback) -> bool:
            self.owner._depth -= 1
            if not self.owner._depth:
                if kind is None:
                    self.owner._conn.commit()
                else:
                    self.owner._conn.rollback()
            return False


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def next_number(conn: sqlite3.Connection, scope: str, partner: str = "*") -> int:
    """The next number in a range, per partner.

    Interchange and group control numbers belong to the *pair* of partners, so
    they are kept per partner; the document numbers are ours and use `*`.
    """
    row = conn.execute(
        "SELECT value FROM control_number WHERE partner = ? AND scope = ?",
        (partner, scope)).fetchone()
    if row is None:
        value = RANGE_START.get(scope, 1)
        conn.execute(
            "INSERT INTO control_number (partner, scope, value) VALUES (?, ?, ?)",
            (partner, scope, value))
    else:
        value = int(row["value"]) + 1
        conn.execute(
            "UPDATE control_number SET value = ? WHERE partner = ? AND scope = ?",
            (value, partner, scope))
    conn.commit()
    return value


def now() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def money(value) -> str:
    """Two decimal places, always - the shape every monetary column is stored in."""
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


def rows(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: Sequence = ()) -> Optional[Dict[str, Any]]:
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

BEHAVIOURS = {
    "accept": "Acknowledge everything in full and ship what was ordered.",
    "short-ship": "Confirm less than was ordered on some lines (855 IQ), and "
                  "ship and invoice the confirmed quantity.",
    "reject-line": "Reject one line outright (855 IR) and leave it out of the "
                   "shipment and the invoice.",
    "reject-all": "Acknowledge the syntax, then refuse the order (855 RJ).",
    "no-ack": "Say nothing at all - no 997 and no 855. For testing your "
              "chase-up timer, which is the failure that actually costs money.",
    "duplicate-invoice": "Send the invoice twice, with the same invoice number, "
                         "as a partner with a retry bug does.",
    "strict": "Reject a transaction set for any finding, not only a fatal one.",
}

PARTNERS = [
    # id, name, qualifier, dialect, version, behaviour, address, duns
    ("ACME", "Acme Distribution Inc", "ZZ", "X12", "004010", "accept",
     ("1 Industrial Parkway", "Columbus", "OH", "43215", "US"), "004321789"),
    ("GLOBEX", "Globex Retail Group", "ZZ", "X12", "005010", "short-ship",
     ("4400 Commerce Drive", "Fort Worth", "TX", "76102", "US"), "008812345"),
    ("INITECH", "Initech Supply Co", "01", "X12", "004010", "reject-line",
     ("222 Bishop Ranch", "San Ramon", "CA", "94583", "US"), "007654321"),
    ("EURODIS", "Eurodis Handels GmbH", "14", "EDIFACT", "D:96A:UN", "accept",
     ("Hafenstrasse 12", "Hamburg", "HH", "20457", "DE"), "315522110"),
]

CATALOG = [
    # sku, description, price, uom, stock
    ("WIDGET-001", "Widget, blue, 40mm", "12.50", "EA", 4200),
    ("WIDGET-002", "Widget, red, 40mm", "13.75", "EA", 3100),
    ("GEAR-100", "Spur gear, steel, 40mm", "8.90", "EA", 1800),
    ("GEAR-200", "Spur gear, brass, 60mm", "14.25", "EA", 60),
    ("BRKT-050", "Mounting bracket, 50mm", "4.15", "EA", 9500),
    ("BRKT-075", "Mounting bracket, heavy, 75mm", "6.40", "EA", 240),
    ("CABLE-2M", "Power cable, 2 m", "3.95", "EA", 15000),
    ("CABLE-5M", "Power cable, 5 m", "6.85", "EA", 7400),
    ("PANEL-A4", "Control panel, A4", "89.00", "EA", 320),
    ("PANEL-A3", "Control panel, A3", "124.50", "EA", 35),
    ("SEAL-KIT", "Hydraulic seal kit", "22.30", "CA", 610),
    ("FILTER-X", "Inline filter, type X", "17.60", "EA", 1250),
]


def upc(base: str) -> str:
    """A UPC-A with its check digit, because partners validate them.

    An item number that fails its own checksum is a fine way to discover that
    your mapping dropped a digit, and a mock whose demo data would not survive
    that check is not much of a test.
    """
    digits = [int(ch) for ch in base[:11].rjust(11, "0")]
    total = sum(d * (3 if index % 2 == 0 else 1) for index, d in enumerate(digits))
    return base[:11].rjust(11, "0") + str((10 - total % 10) % 10)


def seed(conn: sqlite3.Connection, seed_value: int = 42, us_id: str = "MOCKEDI") -> None:
    """Partners, a catalogue, and two orders that already ran their course.

    The seeded history is *document state* - it has no interchanges behind it,
    because nobody sent them.  Everything created while the mock is running
    does have its interchange stored, and `/_mock/documents` shows the
    difference.
    """
    if conn.execute("SELECT 1 FROM partner LIMIT 1").fetchone():
        return
    random.seed(seed_value)
    rng = random.Random(seed_value)

    for index, (pid, name, qualifier, dialect, version, behaviour, address,
                duns) in enumerate(PARTNERS):
        street, city, region, postal, country = address
        conn.execute(
            "INSERT INTO partner (id, name, qualifier, dialect, version, behaviour,"
            " street, city, region, postal, country, duns, test)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, name, qualifier, dialect, version, behaviour,
             street, city, region, postal, country, duns, 1))

    for index, (sku, description, price, uom, stock) in enumerate(CATALOG):
        conn.execute(
            "INSERT INTO catalog (sku, description, upc, price, uom, in_stock,"
            " lead_days) VALUES (?,?,?,?,?,?,?)",
            (sku, description, upc("07612340%03d" % index), price, uom, stock,
             rng.choice([2, 3, 5, 7])))

    _seed_history(conn, rng)
    conn.commit()


def _seed_history(conn: sqlite3.Connection, rng: random.Random) -> None:
    """Two finished orders, so the document endpoints are not empty on a cold start."""
    today = datetime.date.today()
    finished = [
        ("ACME", "4500000871", today - datetime.timedelta(days=14),
         [("WIDGET-001", 240), ("BRKT-050", 500)]),
        ("EURODIS", "PO-2026-00412", today - datetime.timedelta(days=9),
         [("PANEL-A4", 12), ("CABLE-5M", 80)]),
    ]
    prices = {sku: price for sku, _desc, price, _uom, _stock in CATALOG}
    descriptions = {sku: desc for sku, desc, _p, _u, _s in CATALOG}
    units = {sku: uom for sku, _d, _p, uom, _s in CATALOG}

    for partner, po_number, ordered_on, lines in finished:
        seller_order = str(next_number(conn, "seller_order"))
        shipment_id = "SHP%d" % next_number(conn, "shipment")
        invoice_number = "INV%d" % next_number(conn, "invoice")
        shipped_on = ordered_on + datetime.timedelta(days=3)
        invoiced_on = shipped_on + datetime.timedelta(days=1)
        row = conn.execute("SELECT * FROM partner WHERE id = ?", (partner,)).fetchone()

        total = Decimal("0.00")
        for index, (sku, quantity) in enumerate(lines, start=1):
            amount = Decimal(prices[sku]) * quantity
            total += amount
            conn.execute(
                "INSERT INTO order_line (po_number, line, sku, upc, description,"
                " quantity, uom, price, status, confirmed, shipped, invoiced,"
                " scheduled_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (po_number, str(index), sku,
                 conn.execute("SELECT upc FROM catalog WHERE sku = ?",
                              (sku,)).fetchone()["upc"],
                 descriptions[sku], str(quantity), units[sku], prices[sku],
                 "IA", str(quantity), str(quantity), str(quantity),
                 shipped_on.isoformat()))

        conn.execute(
            "INSERT INTO purchase_order (po_number, partner, seller_order,"
            " ordered_on, requested_on, currency, status, total, ship_to_name,"
            " ship_to_id, ship_to_street, ship_to_city, ship_to_region,"
            " ship_to_postal, ship_to_country, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (po_number, partner, seller_order, ordered_on.isoformat(),
             (ordered_on + datetime.timedelta(days=10)).isoformat(),
             "EUR" if partner == "EURODIS" else "USD", "invoiced", money(total),
             row["name"], partner, row["street"], row["city"], row["region"],
             row["postal"], row["country"], now()))

        conn.execute(
            "INSERT INTO shipment (shipment_id, po_number, partner, shipped_on,"
            " carrier, scac, tracking, bol, cartons, weight, at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (shipment_id, po_number, partner, shipped_on.isoformat(),
             "United Parcel Service", "UPSN",
             "1Z%09d" % rng.randrange(10 ** 8, 10 ** 9), str(next_number(conn, "bol")),
             max(1, sum(q for _s, q in lines) // 24),
             str(sum(q for _s, q in lines) * 2), now()))

        tax = (total * Decimal("0.00")).quantize(Decimal("0.01"))
        conn.execute(
            "INSERT INTO invoice (invoice_number, po_number, partner, shipment_id,"
            " invoiced_on, currency, subtotal, tax, total, terms_days,"
            " discount_pct, discount_days, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (invoice_number, po_number, partner, shipment_id, invoiced_on.isoformat(),
             "EUR" if partner == "EURODIS" else "USD", money(total), money(tax),
             money(total + tax), 30, "2", 10, now()))
