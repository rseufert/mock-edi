"""The mock's clock and what waits on it: work promised, the queue advanced, a document sent on demand, and what nobody has acknowledged.

`advance` and `send` are POSTs, and refuse another method in the words they
always used.
"""
from __future__ import annotations

from typing import Tuple

from .. import db, partners, pipeline, reconcile
from . import BadQuery, first, flag, json_body, limit, number, only, route


@route("GET", "/_mock/scheduled")
def scheduled(h) -> Tuple[int, int]:
    # Work the seller has promised but not done: the despatch that is
    # not packed yet, the invoice that is not written yet. Distinct
    # from the outbox, which holds documents that already exist.
    clause = "" if flag(h.query, "all") else " WHERE done_at = ''"
    return h.json(200, db.rows(
        h.mock.conn, "SELECT id, partner, po_number, kind, due_at, done_at,"
                     " note, at FROM scheduled%s ORDER BY due_at, id LIMIT ?"
                     % clause, (limit(h.query),)))


@route("POST", "/_mock/advance", refuse="POST to advance the queue")
def advance(h) -> Tuple[int, int]:
    try:
        only(h.query, "seconds", "all", "failed", "partner")
    except BadQuery as error:
        raise BadQuery(error.parameter, "%s (partner goes with failed)%s"
                       % (error, _in_seconds(h.query))) from None
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


def _in_seconds(query) -> str:
    """What `days=N` would be in seconds, for the caller who came from a
    clock that takes days. Nothing, where there is no figure to give."""
    try:
        days = float(first(query, "days"))
    except ValueError:
        return ""
    # Only a figure `seconds` would itself accept: not nan, not negative,
    # and not past how far the clock may be moved.
    if not 0 <= days <= pipeline.MAX_ADVANCE.days:
        return ""
    return ". The clock moves in seconds: days=%g is seconds=%d" % (
        days, round(days * 86400))


@route("POST", "/_mock/send", refuse="POST to send a document")
def send(h) -> Tuple[int, int]:
    payload = json_body(h.body)
    try:
        queued = h.mock.pipeline.send_document(
            payload.get("partner", ""), payload.get("kind", ""),
            payload.get("order", "") or payload.get("po", ""),
            int(payload.get("delayMs", 0)),
            str(payload.get("shipment", "") or ""))
    except partners.UnknownPartner as error:
        return h.json(404, {"error": "no partner %s" % error})
    except ValueError as error:
        return h.json(400, {"error": str(error)})
    return h.json(201, {"id": queued.id, "kind": queued.kind,
                        "code": queued.code, "dueAt": queued.due_at})


@route("GET", "/_mock/unacknowledged")
def unacknowledged(h) -> Tuple[int, int]:
    return h.json(200, reconcile.unacknowledged(
        h.mock.conn, number(h.query, "older-than"),
        first(h.query, "partner"), limit(h.query)))
