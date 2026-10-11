"""The mock's clock and what waits on it: work promised, the queue advanced, a document sent on demand, and what nobody has acknowledged.

`advance` and `send` are POSTs, and refuse another method in the words they
always used.
"""
from __future__ import annotations

from typing import Tuple

from .. import db, partners, pipeline, reconcile
from . import (BadQuery, first, flag, json_body, limit, number, only, route,
               text, whole)


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
        only(h.query, "seconds", "to", "all", "failed", "partner")
    except BadQuery as error:
        raise BadQuery(error.parameter, "%s (partner goes with failed)%s"
                       % (error, _in_seconds(h.query))) from None
    if "to" in h.query:
        clash = _clashes_with_to(h.query)
        if clash:
            return h.json(400, {"error": clash, "parameter": "to"})
        try:
            released = h.mock.pipeline.advance_to(first(h.query, "to"))
        except ValueError as error:
            return h.json(400, {"error": str(error), "parameter": "to"})
        return _where_the_clock_is(h, released)
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
    return _where_the_clock_is(h, released)


def _where_the_clock_is(h, released) -> Tuple[int, int]:
    """The one answer every advance gives, whichever way it was asked for."""
    h.mock.prune()
    clock = h.mock.pipeline
    return h.json(200, {
        "released": released, "count": len(released),
        "clock": pipeline.clock_text(clock.now()),
        "advancedSeconds": clock.offset.total_seconds()})


def _clashes_with_to(query) -> str:
    """Why `to` cannot be given beside another parameter, or nothing.

    `seconds` and `all` are two more answers to the one question `to`
    answers - where the clock should stop - so taking both would mean
    choosing one without saying so. `failed` moves no clock at all, so a
    `to` beside it is a parameter that does nothing, which is what `only`
    exists to refuse (#203). Empty or not: a caller who wrote `seconds=`
    meant to pass seconds and built the query wrong, and that is worth
    saying rather than guessing at (#341).
    """
    for name in ("seconds", "all"):
        if name in query:
            return ("to and %s each say where the clock stops; give one, not "
                    "both" % name)
    if "failed" in query:
        return ("failed releases what a listener missed and moves no clock, "
                "so to has nothing to do beside it")
    return ""


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
            text(payload, "partner"), text(payload, "kind"),
            text(payload, "order") or text(payload, "po"),
            whole(payload, "delayMs"), text(payload, "shipment"))
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
