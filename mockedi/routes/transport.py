"""The doors an interchange comes through: AS2, its MDNs, and plain HTTP.

* `POST /as2` - the real thing, with AS2 headers and an MDN back.  Use this
  when what you are testing is your AS2 client.
* `POST /edi` - the same pipeline with none of the ceremony, answering with a
  JSON summary of what the mock made of the document.  Use this when what you
  are testing is your *mapping*, and AS2 is just in the way.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Dict, List, Tuple

from .. import as2, charsets, claims, db, delivery
from . import route


@route("POST", "/as2", aliases=("/as2/", "/as2/receive"),
       refuse="POST an interchange here")
def as2_inbound(h) -> Tuple[int, int]:
    body = h.body
    inbound = as2.read(h.headers)
    if not inbound.receiver and not inbound.sender:
        return h.text(400, "this endpoint expects AS2 headers; POST to "
                           "/edi for a plain interchange")

    if inbound.secured:
        # Said plainly rather than mangled: see the note in as2.py.
        return _mdn(h, inbound, body, as2.UNSUPPORTED,
                    "This mock does not implement S/MIME. Send the "
                    "payload unsigned and unencrypted, or use an AS2 "
                    "gateway in front of it.")
    if (h.config.strict_receiver and inbound.receiver
            and inbound.receiver != h.config.as2_id):
        return _mdn(h, inbound, body, as2.ERROR,
                    "AS2-To is %r; this mock answers to %r."
                    % (inbound.receiver, h.config.as2_id))
    if inbound.async_url and not delivery.permitted(inbound.async_url,
                                                    h.config.deliver_to):
        # `/as2` cannot require authentication and still be AS2, so
        # `Receipt-Delivery-Option` is a URL an unauthenticated sender
        # chose. With --deliver-to set, one outside it is refused here
        # rather than posted to - and refused in an MDN the sender gets
        # now, because the address it named is the one we will not use.
        return _mdn(h, dataclasses.replace(inbound, async_url=""),
                    body, as2.FAILED,
                    "Receipt-Delivery-Option names %s, which is not a "
                    "host this mock is allowed to post to. It was "
                    "started with --deliver-to, and the MDN is "
                    "returned here instead." % inbound.async_url)
    if inbound.wants_signed_receipt:
        # RFC 4130: a receiver that cannot produce the signed receipt the
        # sender required answers with a failure, not with an unsigned
        # success the sender has already said it will not accept. The
        # interchange is not read, for the same reason a refused envelope
        # is not: the sender has to send it again knowing the terms.
        return _mdn(h, inbound, body, as2.FAILED,
                    "This mock does not sign: it has no S/MIME and no "
                    "certificate, and a signature it cannot produce is "
                    "not one it will pretend to. Ask for "
                    "signed-receipt-protocol=optional, or put a real "
                    "AS2 gateway in front of it.")

    mic = as2.mic(body, inbound.micalg) if body else ""
    receipts = h.mock.pipeline.receive(
        body, transport="as2", message_id=inbound.message_id, mic=mic,
        charset=charsets.from_content_type(h.headers.get("Content-Type", "")))
    if not any(receipt.ok for receipt in receipts):
        return _mdn(h, inbound, body, as2.ERROR, receipts[0].error)

    # One MDN answers the whole payload, so it is only "processed" when
    # every interchange in it was: a file half of which was refused has
    # not been processed, whatever the other half did.
    explanation = _delivery_text(receipts)
    disposition = (as2.PROCESSED if all(r.accepted for r in receipts)
                   else as2.ERROR)
    return _mdn(h, inbound, body, disposition, explanation)


def _mdn(h, inbound: as2.Inbound, body: bytes, disposition: str,
         explanation: str) -> Tuple[int, int]:
    """Answer an AS2 POST: an MDN now, an MDN later, or neither."""
    if not h.config.mdn or not inbound.wants_mdn:
        return h.text(200, explanation)

    headers, payload = as2.build_mdn(
        inbound, body, h.config.as2_id, disposition, explanation,
        user_agent="mock-edi", moment=h.mock.pipeline.now())
    partner = inbound.sender or "unknown"

    if inbound.asynchronous:
        cursor = h.mock.conn.execute(
            "INSERT INTO mdn (partner, direction, original_id, message_id,"
            " disposition, mic, mode, url, status, payload, headers, at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (partner, "out", inbound.message_id, headers["Message-ID"],
             disposition, as2.mic(body, inbound.micalg) if body else "",
             "async", inbound.async_url, "pending",
             payload.decode("utf-8", "replace"),
             json.dumps(headers), db.now()))
        h.mock.conn.commit()
        h.mock.courier.enqueue_mdn(int(cursor.lastrowid))
        return h.text(202, "MDN will be posted to %s" % inbound.async_url)

    h.mock.conn.execute(
        "INSERT INTO mdn (partner, direction, original_id, message_id,"
        " disposition, mic, mode, status, payload, at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (partner, "out", inbound.message_id, headers["Message-ID"],
         disposition, as2.mic(body, inbound.micalg) if body else "",
         "sync", "sent", payload.decode("utf-8", "replace"), db.now()))
    h.mock.conn.commit()
    return h.raw(200, payload, headers)


@route("POST", "/as2/mdn", aliases=("/as2/mdn/",), refuse="POST an MDN here")
def as2_mdn(h) -> Tuple[int, int]:
    """A partner acknowledging something the mock sent."""
    inbound = as2.read(h.headers)
    fields = as2.parse_mdn(h.body)
    h.mock.conn.execute(
        "INSERT INTO mdn (partner, direction, original_id, message_id,"
        " disposition, mic, mode, status, payload, at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (inbound.sender or "unknown", "in",
         fields.get("Original-Message-ID", ""), inbound.message_id,
         fields.get("Disposition", ""), fields.get("Received-Content-MIC", ""),
         "async", "received", h.body.decode("utf-8", "replace"), db.now()))
    h.mock.conn.commit()
    return h.json(200, {"recorded": True, "fields": fields})


@route("POST", "/edi", aliases=("/edi/",), refuse="POST an interchange here")
def plain_inbound(h) -> Tuple[int, int]:
    receipts = h.mock.pipeline.receive(
        h.body, transport="http",
        charset=charsets.from_content_type(h.headers.get("Content-Type", "")))
    if len(receipts) == 1 and not receipts[0].ok:
        return h.json(422, {"accepted": False, "error": receipts[0].error})

    # A payload holding several interchanges answers with the same keys it
    # always has, summed over the lot, and `interchanges` holds them one by
    # one. For the usual payload of one that is the same object twice, so
    # nothing reading this needs to know about the case until it meets it.
    each = [_interchange_summary(receipt) for receipt in receipts]
    summary = dict(each[0])
    summary.update({
        "accepted": all(item["accepted"] for item in each),
        "orders": [po for item in each for po in item["orders"]],
        "transactionSets": [t for item in each for t in item["transactionSets"]],
        "queued": [q for item in each for q in item["queued"]],
        "acknowledged": [a for item in each for a in item["acknowledged"]],
        "changed": [c for item in each for c in item["changed"]],
        "refusals": [r for item in each for r in item["refusals"]],
        "filed": [f for item in each for f in item["filed"]],
        "disagreements": [d for item in each for d in item["disagreements"]],
        "interchanges": each,
    })
    return h.json(200, summary)


# ---------------------------------------------------------------------------
# What the doors say about what came in
# ---------------------------------------------------------------------------

def findings(message_report) -> List[str]:
    out: List[str] = []
    for finding in message_report.segments:
        for element in finding.elements:
            out.append("%s@%d: %s" % (finding.tag, finding.position, element.note))
        if not finding.elements:
            out.append("%s@%d: %s" % (finding.tag, finding.position, finding.note))
    out.extend(note for _code, note in message_report.set_errors)
    return out


def _interchange_summary(receipt) -> Dict[str, Any]:
    """One interchange's outcome, as the plain endpoint reports it."""
    report = receipt.report
    summary = {
        "accepted": receipt.accepted,
        "partner": receipt.partner,
        "dialect": receipt.dialect,
        "interchange": receipt.control,
        "orders": receipt.orders,
        "transactionSets": [
            {"code": m.code, "control": m.control, "kind": m.kind,
             "accepted": m.accepted, "findings": findings(m),
             "disagreements": [claims.finding_json(d) for d in m.disagreements]}
            for m in (report.messages if report else [])],
        # Business findings, beside the syntax ones and never among them: what
        # a supplier's document says that the order does not (#126).
        "disagreements": [claims.finding_json(d)
                          for m in (report.messages if report else [])
                          for d in m.disagreements],
        "queued": [{"kind": q.kind, "code": q.code, "reference": q.reference,
                    "dueAt": q.due_at} for q in receipt.queued],
        "acknowledged": receipt.acknowledged,
        "changed": receipt.changes,
        "refusals": receipt.refusals,
        "filed": receipt.filed,
    }
    if not receipt.ok:
        # One interchange of several can be refused while the rest are read.
        summary["error"] = receipt.error
    return summary


def _delivery_text(receipts) -> str:
    """The prose an MDN carries about a whole payload."""
    if len(receipts) == 1:
        return _receipt_text(receipts[0])
    parts = ["The payload held %d interchanges." % len(receipts)]
    parts.extend(receipt.error or _receipt_text(receipt) for receipt in receipts)
    return " ".join(parts)


def _receipt_text(receipt) -> str:
    """The prose an MDN carries about what the mock made of the interchange."""
    report = receipt.report
    if report is None:
        return "The interchange was received."
    parts = ["Interchange %s from %s: %d transaction set(s), %d accepted."
             % (receipt.control, receipt.partner, report.received, report.accepted)]
    for message in report.messages:
        if not message.clean:
            parts.append("%s/%s: %s" % (message.code, message.control,
                                        message.summary()))
    if receipt.orders:
        parts.append("Purchase order(s) recorded: %s." % ", ".join(receipt.orders))
    if receipt.queued:
        parts.append("Queued in reply: %s."
                     % ", ".join(q.code for q in receipt.queued))
    return " ".join(parts)
