"""Everything that happened to one purchase order, in the order it happened.

Nothing here is recorded that was not recorded already.  The mock keeps the
interchanges it read and wrote, the transaction sets inside them, the work it
promised, the consignments it packed and the invoices it raised - and to
answer "why did my 810 not match" you had to call four endpoints and sort the
rows into order yourself.

That is the wrong shape for the one question this mock exists to answer.  A
conversation is a sequence, and a tester debugging an integration wants to
read it as one.

Events carry both a structured form and a line of prose, for the same reason
`validate.py` produces findings and `ack.explain` renders them: the machine
needs the first and the person reading a failed test wants the second.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import db, reconcile, schema

# Within one second, the order things must have happened in - for rows
# written before the sequence existed. Timestamps are second-precision on
# purpose (#21), so a great many events share one, and a timeline whose
# order wobbled between calls would be worse than four endpoints.
#
# Ranking by kind of event was all this had until #195, and a rank cannot
# say what happened inside one second, because one second holds several
# steps of several kinds: it put every document the mock sent after the
# packing and invoicing it did, so the timeline said the mock invoiced
# before it acknowledged. `seq` says what actually happened, and these
# remain as the tie-break below it, which is what a database written by an
# older mock needs - every row there has `seq` 0, so it sorts exactly as it
# did rather than arbitrarily.
RANK = {"received": 0, "ordered": 1, "promised": 2, "packed": 3,
        "invoiced": 4, "sent": 5, "acknowledged": 6}
# An order the mock placed runs the other way: the order is recorded, the 850
# goes out, and everything the supplier sends answers it. Documents in one
# second keep the order they were archived in, which is the order they
# happened in.
PLACED_RANK = dict(RANK, ordered=0, sent=1, received=1, acknowledged=2)


def timeline(conn, po_number: str, partner_id: str,
             raw: bool = False) -> Optional[Dict[str, Any]]:
    """The whole conversation about one partner's order, or None if there is none.

    Every row is found by the partner as well as the number: another
    partner's order with the same number is another conversation (#132).
    """
    order = db.one(conn, "SELECT * FROM purchase_order WHERE partner = ?"
                         " AND po_number = ?", (partner_id, po_number))
    if order is None:
        return None

    events: List[Dict[str, Any]] = []
    events.extend(_documents(conn, po_number, partner_id, raw))
    events.append(_ordered(conn, order))
    events.extend(_promised(conn, po_number, partner_id))
    events.extend(_packed(conn, po_number, partner_id))
    events.extend(_invoiced(conn, po_number, partner_id))
    rank = PLACED_RANK if order["direction"] == "placed" else RANK
    # `at` first, so the order and the timestamps a reader can see can never
    # disagree; then the sequence, which is the order things happened in;
    # then the old rank and the row's own id, for rows that have no sequence.
    events.sort(key=lambda event: (event["at"], event.pop("_seq"),
                                   rank[event["event"]], event.pop("_id")))
    return {"order": po_number, "partner": order["partner"],
            "direction": order["direction"], "status": order["status"],
            "events": events}


def _documents(conn, po_number: str, partner_id: str,
               raw: bool) -> List[Dict[str, Any]]:
    """The transaction sets about this order, and the receipts for them.

    An acknowledgment names the interchange it answers, not the order inside
    it, so the 997 that accepted your 850 is found through the envelope rather
    than by the purchase order number. It belongs here even so: "the order
    arrived and was acknowledged" is one thought.

    A receipt coming the *other* way - the partner's 997 for a document the
    mock sent - is reported as an `acknowledged` event at the moment it was
    matched, rather than as another document. What a test wants to know is
    that the 855 was accepted, not that a 997 arrived.
    """
    rows = db.rows(conn,
                   "SELECT t.*, i.transport AS transport, i.control AS envelope,"
                   " i.payload AS payload"
                   " FROM transaction_set t"
                   " LEFT JOIN interchange i ON i.id = t.interchange_id"
                   " WHERE t.partner = ? AND t.reference = ? ORDER BY t.id",
                   (partner_id, po_number))
    envelopes = {row["envelope"] for row in rows if row["envelope"]}
    if envelopes:
        marks = ",".join("?" * len(envelopes))
        rows += db.rows(conn,
                        "SELECT t.*, i.transport AS transport,"
                        " i.control AS envelope, i.payload AS payload"
                        " FROM transaction_set t"
                        " LEFT JOIN interchange i ON i.id = t.interchange_id"
                        " WHERE t.partner = ? AND t.reference IN (%s) AND t.kind IN"
                        " ('acknowledgment', 'interchange-acknowledgment')"
                        " ORDER BY t.id" % marks,
                        (partner_id,) + tuple(sorted(envelopes)))

    out: List[Dict[str, Any]] = []
    for row in rows:
        findings = json.loads(row["findings"] or "[]")
        event = {
            "_id": int(row["id"]),
            "_seq": int(row["seq"]),
            "at": row["at"],
            "event": "received" if row["direction"] == "in" else "sent",
            "direction": row["direction"],
            "code": row["code"],
            "kind": row["kind"],
            "control": row["control"],
            "interchange": row["envelope"] or "",
            "transport": row["transport"] or "",
            "accepted": bool(row["accepted"]),
            "findings": findings,
            "document": int(row["id"]),
        }
        event.update(_carries(row))
        if row["direction"] == "out":
            event["delivery"] = _delivery(conn, row)
        if raw:
            event["payload"] = row["payload"] or ""
        if row["kind"] in reconcile.ACKNOWLEDGMENT_KINDS:
            # Which document this acknowledgment is for. Its own
            # `interchange` above is the envelope it travelled in; the one it
            # answers is the row's reference, which is how it was found, and
            # was then left out of the event (#197).
            event["answers"] = reconcile.answers(
                row["dialect"], row["kind"], row["control"], row["reference"],
                row["payload"] or "")
        if row["direction"] == "in":
            # What this document said that the order does not (#126), on the
            # event that said it rather than in a list of its own.
            # By the envelope as well as the set: a set's own control number
            # is not unique - writers start every interchange at 0001 - so
            # two 855s for one order would otherwise share each other's.
            found = db.rows(conn, "SELECT rule, line, expected, found, note"
                                  " FROM disagreement WHERE partner = ?"
                                  " AND po_number = ? AND code = ? AND control = ?"
                                  " AND interchange = ? ORDER BY id",
                            (partner_id, po_number, row["code"], row["control"],
                             row["envelope"] or ""))
            if found:
                event["disagreements"] = found
        event["summary"] = _summarise(event)
        out.append(event)
        if row["ack_at"]:
            out.append({
                "_id": int(row["id"]),
                "_seq": int(row["ack_seq"]),
                "at": row["ack_at"],
                "event": "acknowledged",
                "direction": "in",
                "code": row["code"],
                "control": row["control"],
                "status": row["ack_status"],
                "acknowledgmentCode": row["ack_code"],
                "note": row["ack_note"],
                "summary": "the partner's receipt %s our %s %s%s"
                           % (row["ack_status"] or "answered", row["code"],
                              row["control"],
                              ": %s" % row["ack_note"] if row["ack_note"] else ""),
            })
    return out


def _carries(row) -> Dict[str, str]:
    """Which business document a sent or received set is (#273).

    In the names the business events beside it use, so a reader can match
    the 810 to its `invoiced` event by a field and not by where it sits:
    `order` on anything about the order, `shipment` on a despatch advice,
    `invoice` and the `shipment` it bills on an invoice. An acknowledgment
    has `answers` instead; it is about an interchange, not an order.

    The shipment and the invoice were recorded when the document was
    written or read. A row from before they were is empty there, and says
    so with an empty field rather than a guess from its payload.
    """
    if row["kind"] in reconcile.ACKNOWLEDGMENT_KINDS:
        return {}
    carries = {"order": row["reference"]}
    if row["kind"] == schema.DESPATCH:
        carries["shipment"] = row["shipment_id"]
    elif row["kind"] == schema.INVOICE:
        carries["invoice"] = row["invoice_number"]
        carries["shipment"] = row["shipment_id"]
    return carries


def _delivery(conn, row) -> Dict[str, Any]:
    """How the document actually got there, when the partner has a URL.

    Matched on the transaction set's own control number, because that is what
    distinguishes two 810s for one order - the second consignment's invoice is
    a different document with the same reference. On the row's own reference
    rather than the order's, because an acknowledgment names the envelope it
    answers and not the order inside it.
    """
    sent = db.one(conn,
                  "SELECT status, attempts, last_error, delivered_at, delivery"
                  " FROM outbound WHERE partner = ? AND reference = ? AND code = ?"
                  " AND set_control = ? ORDER BY id LIMIT 1",
                  (row["partner"], row["reference"], row["code"], row["control"]))
    if sent is None:
        return {}
    return {"status": sent["status"], "attempts": sent["attempts"],
            "lastError": sent["last_error"], "at": sent["delivered_at"],
            "url": sent["delivery"]}


def _ordered(conn, order) -> Dict[str, Any]:
    lines = db.rows(conn, "SELECT * FROM order_line WHERE partner = ?"
                          " AND po_number = ?", (order["partner"], order["po_number"]))
    return {
        "_id": 0,
        "_seq": int(order["seq"]),
        "at": order["at"],
        "event": "ordered",
        "direction": "",
        "orderDirection": order["direction"],
        "lines": len(lines),
        "total": order["total"],
        "currency": order["currency"],
        "summary": "order %s: %d line(s), %s %s"
                   % ("placed with %s" % order["partner"]
                      if order["direction"] == "placed" else "recorded",
                      len(lines), order["total"], order["currency"]),
    }


def _promised(conn, po_number: str, partner_id: str) -> List[Dict[str, Any]]:
    """Work the seller took on and had not done yet.

    A delay postpones the *work*, not the posting, so a promise is a real
    event with a gap after it - and that gap is what a test about timing is
    about.
    """
    out = []
    for row in db.rows(conn, "SELECT * FROM scheduled WHERE partner = ?"
                             " AND po_number = ? ORDER BY id", (partner_id, po_number)):
        out.append({
            "_id": int(row["id"]),
            "_seq": int(row["seq"]),
            "at": row["at"],
            "event": "promised",
            "direction": "",
            "kind": row["kind"],
            "dueAt": row["due_at"],
            "doneAt": row["done_at"],
            "note": row["note"],
            "summary": "%s promised, due %s%s"
                       % (row["kind"], row["due_at"],
                          "" if row["done_at"] else " (not done yet)"),
        })
    return out


def _packed(conn, po_number: str, partner_id: str) -> List[Dict[str, Any]]:
    out = []
    for index, row in enumerate(db.rows(
            conn, "SELECT * FROM shipment WHERE partner = ? AND po_number = ?"
                  " ORDER BY rowid", (partner_id, po_number))):
        lines = db.rows(conn, "SELECT * FROM shipment_line WHERE shipment_id = ?",
                        (row["shipment_id"],))
        out.append({
            "_id": index,
            "_seq": int(row["seq"]),
            "at": row["at"],
            "event": "packed",
            "direction": "",
            "shipment": row["shipment_id"],
            "lines": len(lines),
            "cartons": row["cartons"],
            "weight": row["weight"],
            "carrier": row["carrier"],
            "tracking": row["tracking"],
            "summary": "packed %s: %d line(s), %s carton(s), %s"
                       % (row["shipment_id"], len(lines), row["cartons"],
                          row["carrier"] or "no carrier"),
        })
    return out


def _invoiced(conn, po_number: str, partner_id: str) -> List[Dict[str, Any]]:
    out = []
    for index, row in enumerate(db.rows(
            conn, "SELECT * FROM invoice WHERE partner = ? AND po_number = ?"
                  " ORDER BY rowid", (partner_id, po_number))):
        out.append({
            "_id": index,
            "_seq": int(row["seq"]),
            "at": row["at"],
            "event": "invoiced",
            "direction": "",
            "invoice": row["invoice_number"],
            "shipment": row["shipment_id"],
            "total": row["total"],
            "currency": row["currency"],
            "summary": "invoiced %s for %s: %s %s"
                       % (row["invoice_number"],
                          row["shipment_id"] or "the order",
                          row["total"], row["currency"]),
        })
    return out


_ANSWERING = {reconcile.ACCEPTED: ("accepting", ""),
              reconcile.ACCEPTED_WITH_ERRORS: ("accepting", ", with errors noted"),
              reconcile.REJECTED: ("rejecting", "")}


def _answering(answers: Dict[str, Any], direction: str) -> str:
    """ "accepting your 850 0001": what an acknowledgment says, and of what."""
    whose = "our" if direction == "in" else "your"
    sets = answers["sets"]
    if not sets:
        verb, tail = _ANSWERING.get(answers["status"], ("answering", ""))
        return "%s %s interchange %s%s" % (verb, whose, answers["interchange"],
                                           tail)
    if len({item["status"] for item in sets}) == 1:
        verb, tail = _ANSWERING.get(sets[0]["status"], ("answering", ""))
        return "%s %s %s%s" % (verb, whose, ", ".join(
            "%s %s" % (item["code"], item["control"]) for item in sets), tail)
    return "answering %s %s" % (whose, ", ".join(
        "%s %s (%s)" % (item["code"], item["control"],
                        item["status"].replace("-", " ")) for item in sets))


def _summarise(event: Dict[str, Any]) -> str:
    verb = "received" if event["direction"] == "in" else "sent"
    line = "%s %s %s (%s)" % (verb, event["code"], event["control"],
                              event["kind"] or "document")
    if event.get("answers"):
        line += " " + _answering(event["answers"], event["direction"])
    if not event["accepted"]:
        line += ", rejected"
    elif event["findings"]:
        line += ", accepted with %d finding(s)" % len(event["findings"])
    if event.get("disagreements"):
        line += "; disagrees with the order: %s" % "; ".join(
            item["note"] for item in event["disagreements"])
    delivery = event.get("delivery") or {}
    if delivery.get("status"):
        line += "; delivery %s" % delivery["status"]
        if delivery.get("attempts", 0) > 1:
            line += " after %d attempts" % delivery["attempts"]
    return line
