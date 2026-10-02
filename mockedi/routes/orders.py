"""Purchase orders: those received and those placed, what became of them, and the mock as buyer.

Each is registered for `ANY` method with the `rest` of the path read as
the control plane's old `if` chain read it (#182); `purchase` refuses anything but a POST, and says so
as it did.
"""
from __future__ import annotations

from typing import List, Tuple

from .. import claims, db, documents, partners, remittance, timeline
from . import ANY, first, flag, json_body, limit, route, split


@route(ANY, "/_mock/orders", rest=True)
def orders(h, rest: List[str]) -> Tuple[int, int]:
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
        order["shipments"] = db.rows(
            conn, "SELECT * FROM shipment WHERE partner = ? AND po_number = ?"
                  " ORDER BY rowid", key)
        order["invoices"] = db.rows(
            conn, "SELECT * FROM invoice WHERE partner = ? AND po_number = ?"
                  " ORDER BY rowid", key)
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
        return h.json(200, order)
    return h.json(200, db.rows(
        conn, "SELECT * FROM purchase_order ORDER BY rowid DESC LIMIT ?",
        (limit(h.query),)))


@route(ANY, "/_mock/purchase", rest=True)
def purchase(h, rest: List[str]) -> Tuple[int, int]:
    # The mock as buyer: place an order with a supplier, or change one.
    if h.method != "POST":
        return h.text(405, "POST an order to place it")
    conn = h.mock.conn
    payload = json_body(h.body)
    try:
        if not rest:
            order, queued = h.mock.pipeline.place(
                str(payload.get("partner") or ""), payload)
            status = 201
        elif len(rest) == 2 and rest[1] == "change":
            order, refusal = which_order(conn, rest[0], h.query,
                                         documents.PLACED)
            if order is None:
                return h.json(*refusal)
            order, queued = h.mock.pipeline.change_placed(
                rest[0], order["partner"], payload)
            status = 200
        else:
            return h.json(404, {"error": "no route for %s %s"
                                         % (h.method, split(h.path)[0])})
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
    return h.json(status, order)


@route(ANY, "/_mock/disagreements", rest=True)
def disagreements(h, rest: List[str]) -> Tuple[int, int]:
    # Only the business findings, for a test that wants nothing else.
    return h.json(200, [claims.as_json(row) for row in
                        claims.disagreements(
                            h.mock.conn, first(h.query, "partner"),
                            first(h.query, "po"), limit(h.query, 1000))])


@route(ANY, "/_mock/remittances", rest=True)
def remittances(h, rest: List[str]) -> Tuple[int, int]:
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
