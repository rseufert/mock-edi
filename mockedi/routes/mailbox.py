"""What the mock has to say: the mailbox a partner collects from, the outbox, and the drop directory.

A retry and a scan are POSTs, and refuse another method in the words they
always used. So is a step of a held mock's deliveries.
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


@route("POST", "/_mock/deliver", refuse="POST to deliver what is held")
def deliver(h) -> Tuple[int, int]:
    """Send the next thing a held mock is holding, or all of it with `?all`.

    The answer says what moved and how it ended; `sent` is null when there
    was nothing to send, so a loop over this ends. One step is one POST of
    the mock's own: a document, or an asynchronous MDN, which is its own
    send and is stepped on its own.
    """
    if not h.config.hold_delivery:
        return h.json(409, {
            "error": "delivery is not held: this mock posts each document as "
                     "soon as it is released. Start it with --hold-delivery "
                     "to send one at a time"})
    everything = flag(h.query, "all")
    sent = []
    # The courier records each delivery under the lock this request holds.
    with h.mock.unlocked():
        while True:
            moved = h.mock.courier.step()
            if moved is None:
                break
            sent.append(moved)
            if not everything:
                break
    waiting = h.mock.courier.waiting()
    if everything:
        return h.json(200, {"sent": sent, "count": len(sent), "waiting": waiting})
    return h.json(200, {"sent": sent[0] if sent else None, "waiting": waiting})


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
