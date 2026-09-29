"""The control plane's own endpoints: health, state, behaviours, requests, reset.

Each is registered for `ANY` method with the `rest` of the path ignored,
because `_control` answered them that way (#182); #188 decides whether they
should be stricter. `reset` alone refuses a method, and says so as it did.

The rest of `/_mock` is still answered by `Handler._control`, through the
catch-all at the foot of this module, until #182's last step removes it.
"""
from __future__ import annotations

import sqlite3
from typing import List, Tuple

from .. import db, partners
from . import ANY, CONTROL, limit, route, split


@route(ANY, "/_mock/health", rest=True)
def health(h, rest: List[str]) -> Tuple[int, int]:
    conn = h.mock.conn
    return h.json(200, {
        "status": "ok", "as2Id": h.config.as2_id,
        "started": db.stamp(h.mock.started),
        "partners": _count(conn, "partner"),
        "queued": _count(conn, "outbound", "status = 'ready'"),
    })


@route(ANY, "/_mock/state", rest=True)
def state(h, rest: List[str]) -> Tuple[int, int]:
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


@route(ANY, "/_mock/behaviours", rest=True)
def behaviours(h, rest: List[str]) -> Tuple[int, int]:
    return h.json(200, partners.BEHAVIOURS)


@route(ANY, "/_mock/requests", rest=True)
def requests(h, rest: List[str]) -> Tuple[int, int]:
    return h.json(200, db.rows(
        h.mock.conn, "SELECT * FROM request_log ORDER BY id DESC LIMIT ?",
        (limit(h.query),)))


@route(ANY, "/_mock/reset", rest=True)
def reset(h, rest: List[str]) -> Tuple[int, int]:
    if h.method != "POST":
        return h.text(405, "POST to reset")
    h.mock.reset()
    return h.json(200, {"reset": True})


def _count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    sql = "SELECT COUNT(*) AS n FROM %s%s" % (table, " WHERE " + where if where else "")
    return int(conn.execute(sql).fetchone()["n"])


@route(ANY, CONTROL, rest=True)
def control(h, rest: List[str]) -> Tuple[int, int]:
    """Every `/_mock` path no module above has taken, as `_control` answers it."""
    return h._control(h.method, split(h.path)[0], h.query, h.body)
