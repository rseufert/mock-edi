-- The database schema of mock-edi 0.1.0, verbatim from the v0.1.0 tag.
-- Used to check that a file made by that version still opens.
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
    reference      TEXT NOT NULL DEFAULT '',
    accepted       INTEGER NOT NULL DEFAULT 1,
    findings       TEXT NOT NULL DEFAULT '',
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
    control      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    due_at       TEXT NOT NULL,
    released_at  TEXT NOT NULL DEFAULT '',
    delivered_at TEXT NOT NULL DEFAULT '',
    delivery     TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS ix_outbound_status ON outbound (status, due_at);
CREATE INDEX IF NOT EXISTS ix_line_po ON order_line (po_number);
