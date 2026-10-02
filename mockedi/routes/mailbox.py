"""What the mock has to say: the mailbox a partner collects from, the outbox, and the drop directory.

A retry and a scan are POSTs, and refuse another method in the words they
always used.
"""
from __future__ import annotations

from typing import Tuple

from .. import db
from . import first, flag, limit, route

TEXT = "text/plain; charset=utf-8"


@route("GET", "/_mock/mailbox")
def mailbox(h) -> Tuple[int, int]:
    rows = h.mock.pipeline.collect(
        partner_id=first(h.query, "partner"), kind=first(h.query, "kind"),
        leave=flag(h.query, "leave"))
    if flag(h.query, "raw"):
        joined = b"\n".join(h.mock.pipeline.wire(row)[0] for row in rows)
        return h.raw(200, joined, {"Content-Type": TEXT})
    # Without the mock's own bookkeeping (#195). These rows are answered
    # whole, so the sequence would otherwise reach everyone who collects the
    # mailbox - `mockedi.testing`'s `mailbox()` included. Dropped here rather
    # than in `Pipeline.collect`, so that the shaping sits where the other two
    # endpoints that answer rows whole do theirs.
    return h.json(200, db.public(rows))


@route("GET", "/_mock/outbox")
def outbox(h) -> Tuple[int, int]:
    return h.json(200, db.rows(
        h.mock.conn, "SELECT id, partner, dialect, code, kind, reference, status,"
                     " message_id, control, due_at, released_at, delivered_at,"
                     " delivery, note, attempts, last_error, last_attempt_at,"
                     " at FROM outbound ORDER BY id DESC LIMIT ?",
        (limit(h.query),)))


@route("POST", "/_mock/outbox/<id>/retry", refuse="POST to retry a delivery")
def retry(h, identifier: str) -> Tuple[int, int]:
    conn = h.mock.conn
    try:
        outbound_id = int(identifier)
    except ValueError:
        return h.json(404, {"error": "no outbound document %r" % identifier})
    row = db.one(conn, "SELECT status FROM outbound WHERE id = ?",
                 (outbound_id,))
    if row is None:
        return h.json(404, {"error": "no outbound document %d" % outbound_id})
    if row["status"] != "failed":
        return h.json(409, {
            "error": "outbound document %d is %s, not failed; only a "
                     "failed delivery can be retried"
                     % (outbound_id, row["status"])})
    retried = h.mock.pipeline.redeliver(outbound_id)
    return h.json(200, {"retried": retried, "count": len(retried)})


@route("GET", "/_mock/drop")
def drop(h) -> Tuple[int, int]:
    return h.json(200, h.mock.dropbox.state())


@route("POST", "/_mock/drop/scan", refuse="POST to scan the drop directory")
def scan(h) -> Tuple[int, int]:
    if not h.mock.dropbox.drop_dir:
        return h.json(409, {
            "error": "no drop directory is configured; start the "
                     "mock with --drop-dir"})
    found = h.mock.dropbox.scan()
    return h.json(200, {"scanned": len(found),
                        "files": [vars(item) for item in found]})
