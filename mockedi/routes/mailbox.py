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
    row, missing = _outbound(h, identifier)
    if row is None:
        return h.json(*missing)
    if row["status"] != "failed":
        # A delivery that failed is tried again here. One that worked is a
        # different request, and the refusal says where to make it (#262).
        return h.json(409, {
            "error": "outbound document %d is %s, not failed; only a failed "
                     "delivery can be retried. POST /_mock/outbox/%d/resend "
                     "sends a document again unchanged, whatever became of it"
                     % (row["id"], row["status"], row["id"])})
    retried = h.mock.pipeline.redeliver(row["id"])
    return h.json(200, {"retried": retried, "count": len(retried)})


@route("POST", "/_mock/outbox/<id>/resend", refuse="POST to send a document again")
def resend(h, identifier: str) -> Tuple[int, int]:
    """The same bytes and the same control numbers, a second time.

    For a document whose delivery worked: posted to the partner again, or put
    back in the mailbox to be collected again. It is how a test finds out
    whether a listener is idempotent about a control number it has already
    seen, which `retry` cannot show - that one refuses anything that did not
    fail. `was` is what had become of the document before this.
    """
    row, missing = _outbound(h, identifier)
    if row is None:
        return h.json(*missing)
    was = row["status"]
    if was in ("pending", "cancelled"):
        return h.json(409, {
            "error": "outbound document %d is %s: it has not been sent once, "
                     "so it cannot be sent again" % (row["id"], was)})
    if was == "ready":
        # Released and still waiting to be collected or delivered: there is
        # no first time yet for this to be the second of.
        return h.json(200, {"resent": [], "count": 0, "was": was})
    resent = h.mock.pipeline.redeliver(row["id"], again=True)
    return h.json(200, {"resent": resent, "count": len(resent), "was": was})


def _outbound(h, identifier: str):
    """The outbox row a path names: `(row, None)`, or `(None, (404, body))`."""
    try:
        outbound_id = int(identifier)
    except ValueError:
        return None, (404, {"error": "no outbound document %r" % identifier})
    row = db.one(h.mock.conn, "SELECT id, status FROM outbound WHERE id = ?",
                 (outbound_id,))
    if row is None:
        return None, (404, {"error": "no outbound document %d" % outbound_id})
    return row, None


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
