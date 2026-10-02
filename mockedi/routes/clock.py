"""The mock's clock and what waits on it: work promised, the queue advanced, a document sent on demand, and what nobody has acknowledged.

Each is registered for `ANY` method with the `rest` of the path ignored, as
the control plane's old `if` chain answered it (#182). `advance` and `send` refuse anything but a
POST, and say so as they did.
"""
from __future__ import annotations

from typing import List, Tuple

from .. import db, partners, reconcile
from . import ANY, first, flag, json_body, limit, number, route


@route(ANY, "/_mock/scheduled", rest=True)
def scheduled(h, rest: List[str]) -> Tuple[int, int]:
    # Work the seller has promised but not done: the despatch that is
    # not packed yet, the invoice that is not written yet. Distinct
    # from the outbox, which holds documents that already exist.
    clause = "" if flag(h.query, "all") else " WHERE done_at = ''"
    return h.json(200, db.rows(
        h.mock.conn, "SELECT id, partner, po_number, kind, due_at, done_at,"
                     " note, at FROM scheduled%s ORDER BY due_at, id LIMIT ?"
                     % clause, (limit(h.query),)))


@route(ANY, "/_mock/advance", rest=True)
def advance(h, rest: List[str]) -> Tuple[int, int]:
    if h.method != "POST":
        return h.text(405, "POST to advance the queue")
    if flag(h.query, "failed"):
        # Everything a partner's listener missed while it was down,
        # in queue order, unchanged.
        retried = h.mock.pipeline.redeliver(
            partner_id=first(h.query, "partner") or "")
        return h.json(200, {"retried": retried, "count": len(retried)})
    everything = flag(h.query, "all")
    seconds = number(h.query, "seconds")
    try:
        released = h.mock.pipeline.advance(seconds, everything)
    except ValueError as error:
        return h.json(400, {"error": str(error), "parameter": "seconds"})
    h.mock.prune()
    clock = h.mock.pipeline
    return h.json(200, {
        "released": released, "count": len(released),
        "clock": clock.now().isoformat(timespec="seconds"),
        "advancedSeconds": clock.offset.total_seconds()})


@route(ANY, "/_mock/send", rest=True)
def send(h, rest: List[str]) -> Tuple[int, int]:
    if h.method != "POST":
        return h.text(405, "POST to send a document")
    payload = json_body(h.body)
    try:
        queued = h.mock.pipeline.send_document(
            payload.get("partner", ""), payload.get("kind", ""),
            payload.get("order", "") or payload.get("po", ""),
            int(payload.get("delayMs", 0)))
    except partners.UnknownPartner as error:
        return h.json(404, {"error": "no partner %s" % error})
    except ValueError as error:
        return h.json(400, {"error": str(error)})
    return h.json(201, {"id": queued.id, "kind": queued.kind,
                        "code": queued.code, "dueAt": queued.due_at})


@route(ANY, "/_mock/unacknowledged", rest=True)
def unacknowledged(h, rest: List[str]) -> Tuple[int, int]:
    return h.json(200, reconcile.unacknowledged(
        h.mock.conn, number(h.query, "older-than"),
        first(h.query, "partner"), limit(h.query)))
