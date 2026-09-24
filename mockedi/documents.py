"""What the mock decides to do with an order, and the documents that follow.

An order arrives, and a seller has to answer three questions: will I supply
each line, how much of it, and at what price.  This module answers them, and
then builds the shipment and the invoice that follow from the answer.  The
transaction writers in `transactions.py` render those answers into segments;
they do not make them.

The answers are deliberately reproducible.  Given the same catalogue, the
same partner behaviour and the same order, the mock decides the same thing
every time - because a test that asserts "line 2 comes back short" needs that
to be true on the hundredth run as well as the first.

Line status is decided in this order, and the first rule that fires wins:

1. **The item is not in the catalogue.**  `IR`, whatever the behaviour says.
   This is the most common real rejection and it outranks everything.
2. **The partner's behaviour.**  `reject-all` refuses the order, `reject-line`
   refuses the last line, `short-ship` confirms less than was ordered.
3. **The price disagrees.**  The seller bills its own price, and says so with
   `IP`.  Price discrepancies are the commonest EDI dispute there is, and a
   mock that always agreed with the buyer would never let you test one.
4. Otherwise `IA`, accepted as ordered.
"""
from __future__ import annotations

import datetime
import math
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import db
from .transactions import (ACCEPTED, BACKORDERED, REJECTED, SHORT, Order,
                           number, quantity_text)

PRICE_CHANGED = "IP"
UNITS_PER_CARTON = 24
SHORT_SHIP_FRACTION = Decimal("0.8")


def record_order(conn: sqlite3.Connection, partner: Dict[str, Any], order: Order,
                 when: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Store an incoming order, decide every line, and return the stored row.

    A repeat of a purchase order number replaces what was there.  Real
    receivers differ - some reject a duplicate, some treat it as a change -
    and the mock takes the forgiving reading so that re-running a test does
    not need the database thrown away first.  `/_mock/orders` shows the one
    that survived.
    """
    moment = when or datetime.datetime.now()
    seller_order = _existing_seller_order(conn, order.po_number) or str(
        db.next_number(conn, "seller_order"))
    conn.execute("DELETE FROM order_line WHERE po_number = ?", (order.po_number,))

    ship_to = order.ship_to
    decisions = decide(conn, partner, order, moment)
    total = Decimal("0.00")
    for line, (status, confirmed, price, reason, scheduled) in zip(order.lines, decisions):
        total += (confirmed * price).quantize(Decimal("0.01"))
        # The seller knows its own item numbers even when the buyer sent only
        # one of them, and puts both on everything it sends back.
        item = _catalog(conn, line)
        conn.execute(
            "INSERT INTO order_line (po_number, line, sku, upc, description,"
            " quantity, uom, price, ordered_price, status, confirmed, shipped,"
            " invoiced, reason, scheduled_on)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order.po_number, line.number, (item["sku"] if item else line.sku),
             line.upc or (item["upc"] if item else ""),
             line.description or (item["description"] if item else ""),
             quantity_text(line.quantity), line.uom, db.money(price),
             db.money(line.price), status, quantity_text(confirmed),
             "0", "0", reason, scheduled))

    # An order where nothing was confirmed is refused outright; it will never
    # produce a shipment, so it must not sit in "received" for ever.
    status = "received" if any(
        confirmed > 0 for _s, confirmed, _p, _r, _sched in decisions) else "rejected"
    conn.execute(
        "INSERT OR REPLACE INTO purchase_order (po_number, partner, seller_order,"
        " ordered_on, requested_on, currency, status, total, ship_to_name,"
        " ship_to_id, ship_to_street, ship_to_city, ship_to_region,"
        " ship_to_postal, ship_to_country, at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order.po_number, partner["id"], seller_order,
         order.ordered_on.isoformat() if order.ordered_on else "",
         order.requested_on.isoformat() if order.requested_on else "",
         order.currency, status, db.money(total),
         ship_to.name or partner["name"], ship_to.identifier or partner["id"],
         ship_to.street or partner["street"], ship_to.city or partner["city"],
         ship_to.region or partner["region"], ship_to.postal or partner["postal"],
         ship_to.country or partner["country"], db.now()))
    conn.commit()
    return order_row(conn, order.po_number)


def _existing_seller_order(conn: sqlite3.Connection, po_number: str) -> str:
    row = db.one(conn, "SELECT seller_order FROM purchase_order WHERE po_number = ?",
                 (po_number,))
    return row["seller_order"] if row else ""


def decide(conn: sqlite3.Connection, partner: Dict[str, Any], order: Order,
           when: datetime.datetime) -> List[Tuple[str, Decimal, Decimal, str, str]]:
    """One `(status, confirmed, price, reason, scheduled)` per ordered line."""
    behaviour = partner.get("behaviour") or "accept"
    out: List[Tuple[str, Decimal, Decimal, str, str]] = []
    last = len(order.lines) - 1

    for index, line in enumerate(order.lines):
        item = _catalog(conn, line)
        scheduled = (when.date() + datetime.timedelta(
            days=int(item["lead_days"]) if item else 3)).isoformat()

        if item is None:
            out.append((REJECTED, Decimal("0"), line.price,
                        "%s is not in the catalogue" % (line.sku or line.upc or "the item"),
                        ""))
            continue

        price = Decimal(item["price"])
        stock = Decimal(str(item["in_stock"]))

        if behaviour == "reject-all":
            out.append((REJECTED, Decimal("0"), price, "Order refused", ""))
            continue
        if behaviour == "reject-line" and index == last:
            out.append((REJECTED, Decimal("0"), price,
                        "%s is discontinued" % item["sku"], ""))
            continue

        confirmed = line.quantity
        status, reason = ACCEPTED, ""

        if behaviour == "short-ship":
            confirmed = min(line.quantity, stock)
            if confirmed >= line.quantity:
                confirmed = (line.quantity * SHORT_SHIP_FRACTION).to_integral_value()
            confirmed = max(Decimal("1"), confirmed)
        elif stock < line.quantity:
            confirmed = stock

        if confirmed <= 0:
            out.append((BACKORDERED, Decimal("0"), price,
                        "%s is out of stock" % item["sku"], scheduled))
            continue
        if confirmed < line.quantity:
            status = SHORT
            reason = "Confirmed %s of %s; the balance is not available" % (
                quantity_text(confirmed), quantity_text(line.quantity))
        elif line.price and line.price != price:
            status = PRICE_CHANGED
            reason = "Priced at %s, the order said %s" % (
                db.money(price), db.money(line.price))

        out.append((status, confirmed, price, reason, scheduled))
    return out


def _catalog(conn: sqlite3.Connection, line) -> Optional[Dict[str, Any]]:
    """Find the ordered item by SKU, or failing that by UPC.

    Buyers identify items by whichever number they hold, and a seller that
    could only match one of them would reject half of what it can supply.
    """
    if line.sku:
        row = db.one(conn, "SELECT * FROM catalog WHERE sku = ?", (line.sku,))
        if row:
            return row
    if line.upc:
        return db.one(conn, "SELECT * FROM catalog WHERE upc = ?", (line.upc,))
    return None


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

def order_row(conn: sqlite3.Connection, po_number: str) -> Optional[Dict[str, Any]]:
    return db.one(conn, "SELECT * FROM purchase_order WHERE po_number = ?", (po_number,))


def order_lines(conn: sqlite3.Connection, po_number: str) -> List[Dict[str, Any]]:
    return db.rows(conn,
                   "SELECT * FROM order_line WHERE po_number = ?"
                   " ORDER BY CAST(line AS INTEGER), line", (po_number,))


def shipment_row(conn: sqlite3.Connection, shipment_id: str) -> Optional[Dict[str, Any]]:
    return db.one(conn, "SELECT * FROM shipment WHERE shipment_id = ?", (shipment_id,))


def latest_shipment(conn: sqlite3.Connection, po_number: str) -> Dict[str, Any]:
    return db.one(conn, "SELECT * FROM shipment WHERE po_number = ?"
                        " ORDER BY rowid DESC LIMIT 1", (po_number,)) or {}


# ---------------------------------------------------------------------------
# The documents that follow an accepted order
# ---------------------------------------------------------------------------

def create_shipment(conn: sqlite3.Connection, po_number: str,
                    when: Optional[datetime.datetime] = None) -> Optional[Dict[str, Any]]:
    """Ship what was confirmed. Nothing confirmed means no shipment at all."""
    moment = when or datetime.datetime.now()
    order = order_row(conn, po_number)
    if order is None:
        return None
    lines = order_lines(conn, po_number)
    shipping = [row for row in lines if number(row["confirmed"]) > 0]
    if not shipping:
        conn.execute("UPDATE purchase_order SET status = 'rejected' WHERE po_number = ?",
                     (po_number,))
        conn.commit()
        return None

    units = sum(number(row["confirmed"]) for row in shipping)
    shipment_id = "SHP%d" % db.next_number(conn, "shipment")
    for row in shipping:
        conn.execute("UPDATE order_line SET shipped = ? WHERE po_number = ? AND line = ?",
                     (row["confirmed"], po_number, row["line"]))

    conn.execute(
        "INSERT INTO shipment (shipment_id, po_number, partner, shipped_on, carrier,"
        " scac, tracking, bol, cartons, weight, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (shipment_id, po_number, order["partner"], moment.date().isoformat(),
         "United Parcel Service", "UPSN", _tracking(shipment_id),
         str(db.next_number(conn, "bol")),
         max(1, int(math.ceil(float(units) / UNITS_PER_CARTON))),
         quantity_text(units * 2), db.now()))
    conn.execute("UPDATE purchase_order SET status = 'shipped' WHERE po_number = ?",
                 (po_number,))
    conn.commit()
    return shipment_row(conn, shipment_id)


def _tracking(shipment_id: str) -> str:
    """A tracking number derived from the shipment, so it is reproducible.

    UPS's 1Z format with a checkable-looking body; it is not a real number and
    the carrier will not know it, which is the intended behaviour for a mock.
    """
    digits = "".join(ch for ch in shipment_id if ch.isdigit()).rjust(9, "0")[-9:]
    return "1Z999AA1%s" % digits


def create_invoice(conn: sqlite3.Connection, po_number: str, shipment_id: str = "",
                   when: Optional[datetime.datetime] = None,
                   tax_rate: str = "0") -> Optional[Dict[str, Any]]:
    """Invoice what shipped, at the price the acknowledgment confirmed."""
    moment = when or datetime.datetime.now()
    order = order_row(conn, po_number)
    if order is None:
        return None
    lines = order_lines(conn, po_number)
    billable = [row for row in lines if number(row["shipped"]) > 0]
    if not billable:
        return None

    subtotal = Decimal("0.00")
    for row in billable:
        conn.execute("UPDATE order_line SET invoiced = ? WHERE po_number = ? AND line = ?",
                     (row["shipped"], po_number, row["line"]))
        subtotal += (number(row["shipped"]) * number(row["price"], "0.00")
                     ).quantize(Decimal("0.01"))

    tax = (subtotal * Decimal(tax_rate)).quantize(Decimal("0.01"))
    invoice_number = "INV%d" % db.next_number(conn, "invoice")
    conn.execute(
        "INSERT INTO invoice (invoice_number, po_number, partner, shipment_id,"
        " invoiced_on, currency, subtotal, tax, total, terms_days, discount_pct,"
        " discount_days, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (invoice_number, po_number, order["partner"], shipment_id,
         moment.date().isoformat(), order["currency"], db.money(subtotal),
         db.money(tax), db.money(subtotal + tax), 30, "2", 10, db.now()))
    conn.execute("UPDATE purchase_order SET status = 'invoiced', total = ?"
                 " WHERE po_number = ?", (db.money(subtotal + tax), po_number))
    conn.commit()
    return db.one(conn, "SELECT * FROM invoice WHERE invoice_number = ?",
                  (invoice_number,))


# ---------------------------------------------------------------------------
# Changing an order that has already been sent
#
# The rule that matters, and the one most likely to be wrong in real code:
# **a change cannot unmake what has already happened.**  A quantity cannot be
# lowered below what has shipped, a shipped line cannot be deleted, and an
# order that has been invoiced cannot be changed at all.  Everything else here
# is bookkeeping.
#
# The window in which a change is possible is the window before despatch, so a
# mock running with no delays - where an order is invoiced before the POST
# returns - will refuse every change it is sent.  That is correct behaviour
# and not a limitation to work around: give the mock a despatch delay and the
# window opens.
# ---------------------------------------------------------------------------

REFUSED = "refused"
APPLIED = "applied"

NOT_FOUND = "no such purchase order"
ALREADY_INVOICED = "the order has been invoiced and can no longer be changed"
ALREADY_SHIPPED = "the goods have shipped"


@dataclass
class ChangeOutcome:
    """What the seller did with a change request."""
    po_number: str
    status: str = APPLIED
    reason: str = ""
    cancelled: bool = False
    lines: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return self.status == REFUSED


def apply_change(conn: sqlite3.Connection, partner: Dict[str, Any], change,
                 when: Optional[datetime.datetime] = None) -> ChangeOutcome:
    """Apply a change request to an order the mock already holds."""
    from .transactions import ADD, CHANGE_LINE, DELETE, NO_CHANGE
    moment = when or datetime.datetime.now()
    order = order_row(conn, change.po_number)
    if order is None:
        return ChangeOutcome(change.po_number, REFUSED, NOT_FOUND)
    if order["status"] == "invoiced":
        return ChangeOutcome(change.po_number, REFUSED, ALREADY_INVOICED)

    existing = {row["line"]: row for row in order_lines(conn, change.po_number)}

    if change.cancels:
        shipped = [row for row in existing.values() if number(row["shipped"]) > 0]
        if shipped:
            return ChangeOutcome(
                change.po_number, REFUSED,
                "%s on line %s, so the order cannot be cancelled"
                % (ALREADY_SHIPPED, shipped[0]["line"]))
        for row in existing.values():
            conn.execute(
                "UPDATE order_line SET status = ?, confirmed = '0', reason = ?"
                " WHERE po_number = ? AND line = ?",
                (REJECTED, "Order cancelled at the buyer's request",
                 change.po_number, row["line"]))
        conn.execute("UPDATE purchase_order SET status = 'cancelled', total = ?"
                     " WHERE po_number = ?", ("0.00", change.po_number))
        conn.commit()
        return ChangeOutcome(change.po_number, APPLIED, "Order cancelled",
                             cancelled=True)

    outcome = ChangeOutcome(change.po_number)
    for line in change.lines:
        row = existing.get(line.number)
        action = line.action or CHANGE_LINE

        if action == DELETE:
            outcome.lines.append(_delete_line(conn, change, line, row))
            continue
        if action == NO_CHANGE and row is not None:
            outcome.lines.append({"line": line.number, "action": action,
                                  "status": row["status"], "reason": ""})
            continue
        if row is None or action == ADD:
            outcome.lines.append(_add_line(conn, partner, change, line, moment))
            continue
        outcome.lines.append(_change_line(conn, partner, change, line, row, moment))

    _retotal(conn, change.po_number)
    conn.commit()
    return outcome


def _delete_line(conn, change, line, row) -> Dict[str, Any]:
    if row is None:
        return {"line": line.number, "action": "DI", "status": REJECTED,
                "reason": "there is no line %s to delete" % line.number}
    if number(row["shipped"]) > 0:
        return {"line": line.number, "action": "DI", "status": REJECTED,
                "reason": "%s, so line %s cannot be deleted"
                          % (ALREADY_SHIPPED, line.number)}
    conn.execute(
        "UPDATE order_line SET status = ?, confirmed = '0', reason = ?"
        " WHERE po_number = ? AND line = ?",
        (REJECTED, "Line deleted at the buyer's request", change.po_number,
         line.number))
    return {"line": line.number, "action": "DI", "status": REJECTED,
            "reason": "Line deleted at the buyer's request"}


def _add_line(conn, partner, change, line, moment) -> Dict[str, Any]:
    """A line the order did not have, decided the way a new order line is."""
    from .transactions import Line, Order
    stand_in = Order(po_number=change.po_number, currency=change.currency)
    stand_in.lines.append(Line(number=line.number, sku=line.sku, upc=line.upc,
                               description=line.description,
                               quantity=line.quantity, uom=line.uom,
                               price=line.price))
    status, confirmed, price, reason, scheduled = decide(
        conn, partner, stand_in, moment)[0]
    item = _catalog(conn, line)
    conn.execute(
        "INSERT OR REPLACE INTO order_line (po_number, line, sku, upc,"
        " description, quantity, uom, price, ordered_price, status, confirmed,"
        " shipped, invoiced, reason, scheduled_on)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (change.po_number, line.number, (item["sku"] if item else line.sku),
         line.upc or (item["upc"] if item else ""),
         line.description or (item["description"] if item else ""),
         quantity_text(line.quantity), line.uom, db.money(price),
         db.money(line.price), status, quantity_text(confirmed), "0", "0",
         reason, scheduled))
    return {"line": line.number, "action": "AI", "status": status,
            "reason": reason}


def _change_line(conn, partner, change, line, row, moment) -> Dict[str, Any]:
    """A quantity or a price restated on a line that already exists."""
    from .transactions import CHANGE_LINE
    shipped = number(row["shipped"])
    wanted = line.quantity if line.quantity > 0 else number(row["quantity"])

    # The answer echoes the verb the buyer used - QD, QI, PC - rather than
    # flattening everything to CA, so the buyer can match the response to the
    # request it made.
    action = line.action or CHANGE_LINE
    if wanted < shipped:
        return {"line": line.number, "action": action, "status": REJECTED,
                "reason": "%s of line %s already shipped; the quantity cannot "
                          "be lowered to %s"
                          % (quantity_text(shipped), line.number,
                             quantity_text(wanted))}

    from .transactions import Line, Order
    stand_in = Order(po_number=change.po_number, currency=change.currency)
    stand_in.lines.append(Line(number=line.number, sku=line.sku or row["sku"],
                               upc=line.upc or row["upc"],
                               description=row["description"], quantity=wanted,
                               uom=line.uom or row["uom"],
                               price=line.price or number(row["ordered_price"], "0.00")))
    status, confirmed, price, reason, scheduled = decide(
        conn, partner, stand_in, moment)[0]
    # Never confirm less than has already left the building.
    if confirmed < shipped:
        confirmed = shipped
    conn.execute(
        "UPDATE order_line SET quantity = ?, uom = ?, price = ?,"
        " ordered_price = ?, status = ?, confirmed = ?, reason = ?,"
        " scheduled_on = ? WHERE po_number = ? AND line = ?",
        (quantity_text(wanted), line.uom or row["uom"], db.money(price),
         db.money(line.price or number(row["ordered_price"], "0.00")), status,
         quantity_text(confirmed), reason, scheduled or row["scheduled_on"],
         change.po_number, line.number))
    return {"line": line.number, "action": action, "status": status,
            "reason": reason}


def _retotal(conn: sqlite3.Connection, po_number: str) -> None:
    total = Decimal("0.00")
    for row in order_lines(conn, po_number):
        total += (number(row["confirmed"]) * number(row["price"], "0.00")
                  ).quantize(Decimal("0.01"))
    status = "received" if total > 0 else "cancelled"
    current = order_row(conn, po_number)
    if current and current["status"] in ("shipped", "invoiced"):
        status = current["status"]
    conn.execute("UPDATE purchase_order SET total = ?, status = ?"
                 " WHERE po_number = ?", (db.money(total), status, po_number))
