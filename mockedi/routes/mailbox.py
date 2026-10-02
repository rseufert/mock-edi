"""What the mock has to say: the mailbox a partner collects from, the outbox, and the drop directory.

Each is registered for `ANY` method with the `rest` of the path read as
the control plane's old `if` chain read it (#182). A retry and a scan refuse anything but a POST, and
say so as they did.
"""
from __future__ import annotations

from typing import List, Tuple

from .. import db
from . import ANY, first, flag, limit, route

TEXT = "text/plain; charset=utf-8"


@route(ANY, "/_mock/mailbox", rest=True)
def mailbox(h, rest: List[str]) -> Tuple[int, int]:
    rows = h.mock.pipeline.collect(
        partner_id=first(h.query, "partner"), kind=first(h.query, "kind"),
        leave=flag(h.query, "leave"))
    if flag(h.query, "raw"):
        joined = b"\n".join(h.mock.pipeline.wire(row)[0] for row in rows)
        return h.raw(200, joined, {"Content-Type": TEXT})
    return h.json(200, rows)


@route(ANY, "/_mock/outbox", rest=True)
def outbox(h, rest: List[str]) -> Tuple[int, int]:
    conn = h.mock.conn
    if len(rest) == 2 and rest[1] == "retry":
        if h.method != "POST":
            return h.text(405, "POST to retry a delivery")
        try:
            outbound_id = int(rest[0])
        except ValueError:
            return h.json(404, {"error": "no outbound document %r" % rest[0]})
        row = db.one(conn, "SELECT status FROM outbound WHERE id = ?",
                     (outbound_id,))
        if row is None:
            return h.json(404, {"error": "no outbound document %d"
                                         % outbound_id})
        if row["status"] != "failed":
            return h.json(409, {
                "error": "outbound document %d is %s, not failed; only a "
                         "failed delivery can be retried"
                         % (outbound_id, row["status"])})
        retried = h.mock.pipeline.redeliver(outbound_id)
        return h.json(200, {"retried": retried, "count": len(retried)})
    return h.json(200, db.rows(
        conn, "SELECT id, partner, dialect, code, kind, reference, status,"
              " message_id, control, due_at, released_at, delivered_at,"
              " delivery, note, attempts, last_error, last_attempt_at,"
              " at FROM outbound ORDER BY id DESC LIMIT ?",
        (limit(h.query),)))


@route(ANY, "/_mock/drop", rest=True)
def drop(h, rest: List[str]) -> Tuple[int, int]:
    if rest and rest[0] == "scan":
        if h.method != "POST":
            return h.text(405, "POST to scan the drop directory")
        if not h.mock.dropbox.drop_dir:
            return h.json(409, {
                "error": "no drop directory is configured; start the "
                         "mock with --drop-dir"})
        found = h.mock.dropbox.scan()
        return h.json(200, {"scanned": len(found),
                            "files": [vars(item) for item in found]})
    return h.json(200, h.mock.dropbox.state())
