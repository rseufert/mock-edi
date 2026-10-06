"""The control plane's own endpoints: health, state, behaviours, requests, reset.

A `/_mock` path that nothing is registered for is a 404 that lists what
there is; `routes.not_found` builds it from the table.
"""
from __future__ import annotations

import sqlite3
from typing import Tuple

from .. import db, partners
from . import limit, route


@route("GET", "/_mock/health")
def health(h) -> Tuple[int, int]:
    conn = h.mock.conn
    return h.json(200, {
        "status": "ok", "as2Id": h.config.as2_id,
        "started": db.stamp(h.mock.started),
        "partners": _count(conn, "partner"),
        "queued": _count(conn, "outbound", "status = 'ready'"),
        "deliveryHeld": h.config.hold_delivery,
    })


@route("GET", "/_mock/state")
def state(h) -> Tuple[int, int]:
    conn = h.mock.conn
    return h.json(200, {
        "as2Id": h.config.as2_id,
        "database": h.config.db_path,
        "counts": {name: _count(conn, name) for name in (
            "partner", "catalog", "interchange", "transaction_set",
            "purchase_order", "order_line", "shipment", "invoice",
            "outbound", "scheduled", "mdn")},
        "scheduled": {
            "waiting": _count(conn, "scheduled", "done_at = ''"),
            "done": _count(conn, "scheduled", "done_at != ''")},
        "queue": {status: _count(conn, "outbound", "status = '%s'" % status)
                  for status in ("pending", "ready", "delivered",
                                 "collected", "failed")},
        "delays": {
            "acknowledgment": h.config.ack_delay_ms,
            "response": h.config.response_delay_ms,
            "despatch": h.config.despatch_delay_ms,
            "invoice": h.config.invoice_delay_ms},
        "courierFailures": list(h.mock.courier.failures[-10:]),
        "retention": {
            "keepRequests": h.config.keep_requests,
            "retentionDays": h.config.retention_days,
            "pruned": dict(h.mock.pruned)},
    })


@route("GET", "/_mock/behaviours")
def behaviours(h) -> Tuple[int, int]:
    return h.json(200, partners.BEHAVIOURS)


@route("GET", "/_mock/requests")
def requests(h) -> Tuple[int, int]:
    return h.json(200, db.rows(
        h.mock.conn, "SELECT * FROM request_log ORDER BY id DESC LIMIT ?",
        (limit(h.query),)))


@route("POST", "/_mock/reset", refuse="POST to reset")
def reset(h) -> Tuple[int, int]:
    h.mock.reset()
    return h.json(200, {"reset": True})


def _count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    sql = "SELECT COUNT(*) AS n FROM %s%s" % (table, " WHERE " + where if where else "")
    return int(conn.execute(sql).fetchone()["n"])
