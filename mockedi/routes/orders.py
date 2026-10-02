"""Purchase orders: those received and those placed, what became of them, and the mock as buyer.

An order and its timeline are routes of their own; `purchase` and a change
to a placed order are POSTs, and refuse another method as they always did.
"""
from __future__ import annotations

from typing import Tuple

from .. import claims, db, documents, partners, remittance, timeline
from . import first, flag, json_body, limit, route, text


@route("GET", "/_mock/orders")
@route("GET", "/_mock/orders/<po>")
def orders(h, *rest: str) -> Tuple[int, int]:
    conn = h.mock.conn
    if rest:
        order, refusal = which_order(conn, rest[0], h.query)
        if order is None:
            return h.json(*refusal)
    if len(rest) == 2 and rest[1] == "timeline":
        # Everything that happened to this order, in order. Four
        # endpoints' worth of rows, sorted, which is what anyone
        # debugging one was assembling by hand.
        return h.json(200, timeline.timeline(
            conn, order["po_number"], order["partner"],
            raw=flag(h.query, "raw")))
    if rest:
        key = (order["partner"], order["po_number"])
        order["lines"] = documents.order_lines(conn, order["po_number"],
                                               order["partner"])
        order["shipments"] = db.public(db.rows(
            conn, "SELECT * FROM shipment WHERE partner = ? AND po_number = ?"
                  " ORDER BY rowid", key))
        order["invoices"] = db.public(db.rows(
            conn, "SELECT * FROM invoice WHERE partner = ? AND po_number = ?"
                  " ORDER BY rowid", key))
        if order["direction"] == documents.PLACED:
            # What was asked for beside what the supplier said, line by
            # line, and where the two disagree (#126).
            order["reconciliation"] = [
                {"line": row["line"], "sku": row["sku"],
                 "ordered": row["quantity"], "confirmed": row["confirmed"],
                 "shipped": row["shipped"], "billed": row["invoiced"],
                 "status": row["status"]}
                for row in order["lines"]]
            order["disagreements"] = [
                claims.as_json(row) for row in
                claims.disagreements(conn, *key)]
        return h.json(200, db.public(order))
    return h.json(200, db.public(db.rows(
        conn, "SELECT * FROM purchase_order ORDER BY rowid DESC LIMIT ?",
        (limit(h.query),))))


@route("GET", "/_mock/orders/<po>/timeline")
def order_timeline(h, po: str) -> Tuple[int, int]:
    return orders(h, po, "timeline")


@route("POST", "/_mock/purchase", refuse="POST an order to place it")
def purchase(h, *rest: str) -> Tuple[int, int]:
    # The mock as buyer: place an order with a supplier, or change one.
    conn = h.mock.conn
    payload = json_body(h.body)
    try:
        if not rest:
            order, queued = h.mock.pipeline.place(
                text(payload, "partner"), payload)
            status = 201
        else:
            order, refusal = which_order(conn, rest[0], h.query,
                                         documents.PLACED)
            if order is None:
                return h.json(*refusal)
            order, queued = h.mock.pipeline.change_placed(
                rest[0], order["partner"], payload)
            status = 200
    except partners.UnknownPartner as error:
        return h.json(404, {"error": "no partner %s" % error})
    except LookupError as error:
        return h.json(404, {"error": str(error)})
    except documents.Refused as error:
        return h.json(400, {"error": str(error),
                            "problems": error.problems})
    order["lines"] = documents.order_lines(conn, order["po_number"],
                                           order["partner"])
    order["sent"] = {"id": queued.id, "kind": queued.kind,
                     "code": queued.code, "dueAt": queued.due_at}
    return h.json(status, db.public(order))


@route("POST", "/_mock/purchase/<po>/change",
       refuse="POST an order to place it")
def purchase_change(h, po: str) -> Tuple[int, int]:
    return purchase(h, po)


@route("GET", "/_mock/disagreements")
def disagreements(h) -> Tuple[int, int]:
    # Only the business findings, for a test that wants nothing else.
    return h.json(200, [claims.as_json(row) for row in
                        claims.disagreements(
                            h.mock.conn, first(h.query, "partner"),
                            first(h.query, "po"), limit(h.query, 1000))])


@route("GET", "/_mock/remittances")
def remittances(h) -> Tuple[int, int]:
    # Every remittance advice received, and what became of it.
    return h.json(200, remittance.listing(h.mock.conn, first(h.query, "partner")))


def which_order(conn, po_number: str, query, direction: str = ""):
    """The order a URL names: `(row, None)`, or `(None, (status, body))`.

    A PO number is unique per partner, not to the world (#132), so `?partner=`
    picks one. Without it the number has to be unambiguous; when two partners
    hold it the answer is a 409 naming them, rather than a guess.
    """
    partner_id = first(query, "partner")
    if partner_id:
        found = [documents.order_row(conn, po_number, partner_id)]
        found = [row for row in found if row is not None]
    else:
        found = documents.orders_numbered(conn, po_number)
    if direction:
        found = [row for row in found if row["direction"] == direction]
    what = "purchase order %r%s" % (po_number, " placed" if direction else "")
    if not found:
        return None, (404, {"error": "no %s%s" % (
            what, " with %s" % partner_id if partner_id else "")})
    if len(found) > 1:
        holders = [row["partner"] for row in found]
        return None, (409, {"error": "%s is held by %s; add ?partner= to say whose"
                                     % (what, " and ".join(holders)),
                            "partners": holders})
    return found[0], None
