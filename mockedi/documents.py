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

1. **The line does not ask for anything.**  A quantity of zero or less is
   `IR`, before any behaviour runs.  Nothing downstream can make sense of it,
   and every alternative is worse: confirming it promises goods nobody
   ordered, and backordering it reports a stock problem that does not exist.
2. **The item is not in the catalogue.**  `IR`, whatever the behaviour says.
   This is the most common real rejection.
3. **The partner's behaviour.**  `reject-all` refuses the order, `reject-line`
   refuses the last line, `short-ship` confirms less than was ordered.
4. **There is not enough stock.**  Confirmed is capped at what is on hand,
   which is `IQ` when it falls short and `IB` when there is none at all.
   This cap applies whatever the behaviour, and it outranks the price rule -
   so a line that is both short *and* mispriced is reported `IQ`, with the
   price named in its reason rather than changed in silence.
5. **The price disagrees.**  The seller bills its own price, and says so with
   `IP`.  Price discrepancies are the commonest EDI dispute there is, and a
   mock that always agreed with the buyer would never let you test one.
6. Otherwise `IA`, accepted as ordered.

An order goes one of two ways, and `purchase_order.direction` says which:

- **`received`**: a customer's order, which the mock sells against. Everything
  above applies. `quantity` and `ordered_price` are what the customer asked
  for, `price` is what the mock will bill, and `confirmed`, `shipped` and
  `invoiced` are what the mock *did*.
- **`placed`**: an order the mock sent to a supplier (`place_order`). None of
  the rules above run: the mock is the buyer, and the supplier decides.
  `quantity`, `price` and `ordered_price` are what the mock asked for.
  `confirmed`, `shipped` and `invoiced` stay zero until the supplier's 855,
  856 and 810 are reconciled against them (#126), and then they hold what the
  supplier *claimed*, not anything the mock did.

The seller's machinery - deciding, packing, invoicing, applying a change -
touches only received orders. A placed order is changed by `change_placed`,
which records what the mock asked for and nothing else.
"""
from __future__ import annotations

import datetime
import decimal
import math
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import db
from .envelope import local
from .money import cents, unit_price
from .transactions import (ACCEPTED, BACKORDERED, REJECTED, SHORT, Order,
                           number, quantity_text)

RECEIVED = "received"
PLACED = "placed"
DIRECTIONS = (RECEIVED, PLACED)

# An order in one of these is over: nothing more will be sent for it and
# nothing more can be done to it. Anything else - including a status added
# later - is live, so the conservative answer is the default (#146).
FINISHED = ("invoiced", "cancelled", "rejected")

PRICE_CHANGED = "IP"
UNITS_PER_CARTON = 24
SHORT_SHIP_FRACTION = Decimal("0.8")
# What an over-shipping seller packs for each unit it confirmed (#212).
OVER_SHIP_FRACTION = Decimal("1.3")


def record_order(conn: sqlite3.Connection, partner: Dict[str, Any], order: Order,
                 when: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Store an incoming order, decide every line, and return the stored row.

    A repeat of a purchase order number from the same partner replaces what
    was there.  Real receivers differ - some reject a duplicate, some treat it
    as a change - and the mock takes the forgiving reading so that re-running
    a test does not need the database thrown away first.  `/_mock/orders`
    shows the one that survived.  Another partner's order with the same number
    is a different order, and untouched.
    """
    moment = when or db.moment(conn)
    seller_order = _existing_seller_order(conn, order.po_number, partner["id"]) or str(
        db.next_number(conn, "seller_order"))
    conn.execute("DELETE FROM order_line WHERE partner = ? AND po_number = ?",
                 (partner["id"], order.po_number))

    ship_to = order.ship_to
    decisions = decide(conn, partner, order, moment)
    total = Decimal("0.00")
    for line, (status, confirmed, price, reason, scheduled) in zip(order.lines, decisions):
        total += cents(confirmed * price)
        # The seller knows its own item numbers even when the buyer sent only
        # one of them, and puts both on everything it sends back.
        item = _catalog(conn, line)
        conn.execute(
            "INSERT INTO order_line (partner, po_number, line, sku, upc,"
            " description, quantity, uom, price, ordered_price, status, confirmed,"
            " shipped, invoiced, reason, scheduled_on)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (partner["id"], order.po_number, line.number, (item["sku"] if item else line.sku),
             line.upc or (item["upc"] if item else ""),
             line.description or (item["description"] if item else ""),
             quantity_text(line.quantity), line.uom, unit_price(price),
             unit_price(line.price), status, quantity_text(confirmed),
             "0", "0", reason, scheduled))

    # An order where nothing was confirmed is refused outright; it will never
    # produce a shipment, so it must not sit in "received" for ever.
    status = "received" if any(
        confirmed > 0 for _s, confirmed, _p, _r, _sched in decisions) else "rejected"
    conn.execute(
        "INSERT OR REPLACE INTO purchase_order (po_number, partner, seller_order,"
        " ordered_on, requested_on, currency, status, total, ship_to_name,"
        " ship_to_id, ship_to_street, ship_to_city, ship_to_region,"
        " ship_to_postal, ship_to_country, at, seq)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order.po_number, partner["id"], seller_order,
         order.ordered_on.isoformat() if order.ordered_on else "",
         order.requested_on.isoformat() if order.requested_on else "",
         order.currency, status, db.money(total),
         ship_to.name or partner["name"], ship_to.identifier or partner["id"],
         ship_to.street or partner["street"], ship_to.city or partner["city"],
         ship_to.region or partner["region"], ship_to.postal or partner["postal"],
         ship_to.country or partner["country"], db.now(conn), db.next_seq(conn)))
    conn.commit()
    return order_row(conn, order.po_number, partner["id"])


def _not_text(request: Dict[str, Any], *names: str) -> List[str]:
    """Which of `names` the request gives as something other than a string."""
    return ["%s must be a string, not %s" % (name, db.json_kind(request[name]))
            for name in names
            if name in request and not isinstance(request[name], str)]


class Refused(ValueError):
    """An order the mock will not place or change, with every reason at once."""

    def __init__(self, problems: Sequence[str]):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def place_order(conn: sqlite3.Connection, partner: Dict[str, Any], us,
                request: Dict[str, Any],
                when: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Store an order the mock is placing with a supplier, and return its row.

    `request` is what `POST /_mock/purchase` was given: `lines`, each with a
    `sku` or `upc`, a `quantity` and a `price`, and optionally `po_number`,
    `requested_on`, `currency`. The PO number comes from the mock's own range
    when there is none, because a buyer numbers its own orders. The order
    ships to the mock itself.

    Nothing about deciding runs here - see the module docstring. Writing the
    850 and sending it is the caller's business, so this stays a function of
    the database alone.
    """
    moment = when or db.moment(conn)
    problems: List[str] = _not_text(request, "po_number", "currency",
                                    "requested_on")
    if problems:
        # Nothing below can be said about a value of the wrong type: a list
        # for a PO number was looked up as one, and an object stored as its
        # repr (#207).
        raise Refused(problems)
    po_number = str(request.get("po_number") or "").strip()
    if po_number and order_row(conn, po_number, partner["id"]) is not None:
        problems.append("purchase order %s already exists; change it with "
                        "/_mock/purchase/%s/change" % (po_number, po_number))
    requested_on = str(request.get("requested_on") or "")
    if requested_on:
        try:
            datetime.date.fromisoformat(requested_on)
        except ValueError:
            problems.append("requested_on %r is not a date (YYYY-MM-DD)"
                            % requested_on)
    lines = request.get("lines")
    if not isinstance(lines, list) or not lines:
        problems.append("an order needs at least one line")
        lines = []
    rows = []
    for index, line in enumerate(lines, 1):
        row, line_problems = _placed_line(conn, index, line)
        problems.extend(line_problems)
        rows.append(row)
    if problems:
        raise Refused(problems)

    po_number = po_number or str(db.next_number(conn, "purchase_order"))
    total = Decimal("0.00")
    for row in rows:
        total += cents(row["quantity"] * row["price"])
        conn.execute(
            "INSERT INTO order_line (partner, po_number, line, sku, upc,"
            " description, quantity, uom, price, ordered_price)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (partner["id"], po_number, row["line"], row["sku"], row["upc"],
             row["description"], quantity_text(row["quantity"]), row["uom"],
             unit_price(row["price"]), unit_price(row["price"])))
    conn.execute(
        "INSERT INTO purchase_order (po_number, partner, ordered_on,"
        " requested_on, currency, status, total, ship_to_name, ship_to_id,"
        " ship_to_street, ship_to_city, ship_to_region, ship_to_postal,"
        " ship_to_country, direction, at, seq)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (po_number, partner["id"], local(moment).date().isoformat(), requested_on,
         str(request.get("currency") or "USD"), PLACED, db.money(total),
         us.name, us.identifier, us.street, us.city, us.region, us.postal,
         us.country or "US", PLACED, db.now(conn), db.next_seq(conn)))
    conn.commit()
    return order_row(conn, po_number, partner["id"])


# The verbs a change request is given in, and the 670 codes they are.
CHANGE_ACTIONS = {"add": "AI", "change": "CA", "delete": "DI"}


def _refuse_an_unwritable_change_number(conn, partner_id: str, po_number: str,
                                        sequence: str) -> None:
    """Refuse a change the ORDCHG could not carry the number of (#186).

    An ORDCHG identifies itself by the order's number and the sequence
    together, in BGM's 1004, which D.96A makes `an..35`. A long enough order
    number overflows it, and then the mock would write a document its own
    dictionary reports - the fault #291 is about, one element over. Refused at
    the door instead, the way `/_mock/purchase` refuses what the dictionary
    would reject (#237).

    EDIFACT only. The 860 carries the sequence in `BCH05` on its own and the
    order number in `BCH03`, so nothing is packed together and nothing
    overflows; refusing an X12 change for an EDIFACT limit would be a lie
    about the document being written.
    """
    from . import schema, transactions
    row = conn.execute("SELECT dialect FROM partner WHERE id = ?",
                       (partner_id,)).fetchone()
    if row is None or row["dialect"] != "EDIFACT":
        return
    # The width is the dictionary's, read rather than repeated.
    limit = schema.BGM.elements[1].max_len
    number = transactions.change_number(po_number, sequence)
    if len(number) <= limit:
        return
    raise Refused([
        "an ORDCHG for %s would be numbered %r, which is %d characters; "
        "BGM's 1004 allows %d. A change request is numbered by the order and "
        "the sequence together, so an order number of %d characters leaves "
        "no room for one. Place the order under a shorter number."
        % (po_number, number, len(number), limit, len(po_number))])


def change_placed(conn: sqlite3.Connection, po_number: str, partner_id: str,
                  request: Dict[str, Any],
                  when: Optional[datetime.datetime] = None):
    """Change an order the mock placed, and return the change to send.

    `request` is `{"cancel": true}`, or `lines`, each naming a `line` and an
    `action` - `add`, `change` (the default) or `delete` - with the new
    `quantity` and `price`. What the order now asks for is stored; the
    returned `transactions.Change` is what the 860 or ORDCHG says.
    """
    from .transactions import CANCEL_PURPOSES, Change, ChangeLine
    moment = when or db.moment(conn)
    order = order_row(conn, po_number, partner_id)
    if order is None or order["direction"] != PLACED:
        raise LookupError("the mock placed no purchase order %r" % po_number)
    if order["status"] == "cancelled":
        raise Refused(["purchase order %s is already cancelled" % po_number])
    held = {row["line"]: row for row in order_lines(conn, po_number, partner_id)}
    change = Change(po_number=po_number, changed_on=local(moment).date(),
                    ordered_on=_date(order["ordered_on"]),
                    currency=order["currency"],
                    sequence=str(db.next_number(conn, "purchase_change",
                                                "%s/%s" % (partner_id, po_number))))

    _refuse_an_unwritable_change_number(conn, partner_id, po_number,
                                        change.sequence)

    if not isinstance(request.get("cancel", False), bool):
        # "no" is a string, and a string is true.
        raise Refused(["cancel must be true or false, not %s"
                       % db.json_kind(request["cancel"])])
    if request.get("cancel"):
        change.purpose = CANCEL_PURPOSES[0]
        conn.execute("UPDATE purchase_order SET status = 'cancelled'"
                     " WHERE partner = ? AND po_number = ?", (partner_id, po_number))
        conn.commit()
        return change

    lines = request.get("lines")
    problems: List[str] = []
    if not isinstance(lines, list) or not lines:
        problems.append("a change needs at least one line, or cancel: true")
        lines = []
    wanted = []
    for index, line in enumerate(lines, 1):
        if not isinstance(line, dict):
            problems.append("line %d must be an object, not %s"
                            % (index, db.json_kind(line)))
            continue
        action = CHANGE_ACTIONS.get(str(line.get("action") or "change"))
        if action is None:
            problems.append("line %d: action %r is not one of %s"
                            % (index, line.get("action"), ", ".join(CHANGE_ACTIONS)))
            continue
        wanted_line = str(line.get("line") or "")
        if action == "AI":
            if wanted_line in held:
                problems.append("line %s is already on the order" % wanted_line)
                continue
            row, line_problems = _placed_line(conn, index, line)
            row["line"] = wanted_line or str(max([int(n) for n in held if n.isdigit()]
                                            + [len(held)]) + 1)
        elif wanted_line not in held:
            problems.append("purchase order %s has no line %r to %s"
                            % (po_number, wanted_line, "delete" if action == "DI"
                               else "change"))
            continue
        elif action == "DI":
            row, line_problems = dict(held[wanted_line]), []
            row["quantity"] = number(row["quantity"])
            row["price"] = number(row["price"])
        else:
            merged = {"sku": held[wanted_line]["sku"], "upc": held[wanted_line]["upc"],
                      "description": held[wanted_line]["description"],
                      "uom": held[wanted_line]["uom"],
                      "quantity": held[wanted_line]["quantity"],
                      "price": held[wanted_line]["price"], "line": wanted_line}
            merged.update({k: v for k, v in line.items() if k != "action"})
            row, line_problems = _placed_line(conn, index, merged)
        problems.extend(line_problems)
        wanted.append((action, row))
    remaining = (set(held) - {row["line"] for action, row in wanted if action == "DI"}
                 | {row["line"] for action, row in wanted if action == "AI"})
    if wanted and not remaining:
        problems.append("that deletes every line; send cancel: true to "
                        "withdraw the order")
    if problems:
        raise Refused(problems)

    for action, row in wanted:
        if action == "DI":
            conn.execute("DELETE FROM order_line WHERE partner = ? AND po_number = ?"
                         " AND line = ?", (partner_id, po_number, row["line"]))
        elif action == "AI":
            conn.execute(
                "INSERT INTO order_line (partner, po_number, line, sku, upc,"
                " description, quantity, uom, price, ordered_price)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (partner_id, po_number, row["line"], row["sku"], row["upc"],
                 row["description"], quantity_text(row["quantity"]), row["uom"],
                 unit_price(row["price"]), unit_price(row["price"])))
        else:
            conn.execute(
                "UPDATE order_line SET quantity = ?, uom = ?, price = ?,"
                " ordered_price = ? WHERE partner = ? AND po_number = ? AND line = ?",
                (quantity_text(row["quantity"]), row["uom"], unit_price(row["price"]),
                 unit_price(row["price"]), partner_id, po_number, row["line"]))
        change.lines.append(ChangeLine(
            number=row["line"], sku=row["sku"], upc=row["upc"],
            description=row["description"], quantity=row["quantity"],
            uom=row["uom"], price=row["price"], action=action))
    total = sum((cents(number(row["quantity"]) * number(row["price"], "0.00"))
                 for row in order_lines(conn, po_number, partner_id)), Decimal("0.00"))
    conn.execute("UPDATE purchase_order SET total = ? WHERE partner = ?"
                 " AND po_number = ?", (db.money(total), partner_id, po_number))
    conn.commit()
    return change


def _date(text: str) -> Optional[datetime.date]:
    return datetime.date.fromisoformat(text) if text else None


def _placed_line(conn: sqlite3.Connection, index: int,
                 line: Any) -> Tuple[Dict[str, Any], List[str]]:
    """One requested line, filled in from the catalogue, and what is wrong with it."""
    where = "line %d" % index
    if not isinstance(line, dict):
        return {}, ["%s must be an object, not %s" % (where, db.json_kind(line))]
    problems = []
    sku, upc = str(line.get("sku") or ""), str(line.get("upc") or "")
    if not (sku or upc):
        problems.append("%s names no item: give a sku or a upc" % where)
    values = {}
    for name in ("quantity", "price"):
        try:
            values[name] = Decimal(str(line.get(name, "")))
        except ArithmeticError:
            values[name] = None
        if values[name] is None or not values[name].is_finite():
            problems.append("%s: %s %r is not a number" % (where, name, line.get(name)))
    if values.get("quantity") is not None and values["quantity"] <= 0:
        problems.append("%s: an order for %s of something is not an order"
                        % (where, line.get("quantity")))
    if values.get("price") is not None and values["price"] < 0:
        problems.append("%s: price %s is negative" % (where, line.get("price")))
    # The items the mock sells are the ones it buys, so the catalogue fills in
    # the number the request left out - a supplier's translator may match on
    # either.
    item = (db.one(conn, "SELECT * FROM catalog WHERE sku = ?", (sku,)) if sku
            else db.one(conn, "SELECT * FROM catalog WHERE upc = ?", (upc,)))
    row = {"line": str(line.get("line") or index), "sku": sku or (item or {}).get("sku", ""),
           "upc": upc or (item or {}).get("upc", ""),
           "description": str(line.get("description") or "") or (item or {}).get("description", ""),
           "uom": str(line.get("uom") or "EA"),
           "quantity": values.get("quantity"), "price": values.get("price")}
    return row, problems


def _existing_seller_order(conn: sqlite3.Connection, po_number: str,
                           partner_id: str) -> str:
    row = db.one(conn, "SELECT seller_order FROM purchase_order"
                       " WHERE partner = ? AND po_number = ?", (partner_id, po_number))
    return row["seller_order"] if row else ""


def decide(conn: sqlite3.Connection, partner: Dict[str, Any], order: Order,
           when: datetime.datetime) -> List[Tuple[str, Decimal, Decimal, str, str]]:
    """One `(status, confirmed, price, reason, scheduled)` per ordered line."""
    behaviour = partner.get("behaviour") or "accept"
    out: List[Tuple[str, Decimal, Decimal, str, str]] = []
    last = len(order.lines) - 1

    for index, line in enumerate(order.lines):
        item = _catalog(conn, line)
        scheduled = (local(when).date() + datetime.timedelta(
            days=int(item["lead_days"]) if item else 3)).isoformat()

        if line.quantity <= 0:
            # Before anything else, including the catalogue: a line asking for
            # nothing cannot be confirmed, short-shipped or backordered, and
            # pretending otherwise puts a quantity nobody ordered on a
            # despatch advice.
            out.append((REJECTED, Decimal("0"), line.price,
                        "A quantity of %s was ordered; nothing can be supplied "
                        "against it" % quantity_text(line.quantity), ""))
            continue

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
            # At least one, but never more than was asked for: rounding a
            # fraction of a single unit down to zero would report a stock
            # problem, and rounding it up would ship more than the order.
            # The ceiling goes last, or the floor wins against an order for
            # less than one unit and half a unit is billed as a whole (#206).
            confirmed = min(max(Decimal("1"), confirmed), line.quantity)
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
            if line.price and line.price != price:
                # Both apply, and a line carries one status code. Say the
                # other one out loud rather than changing the price in
                # silence - a buyer reconciling the invoice needs to know.
                reason += "; priced at %s, the order said %s" % (
                    unit_price(price), unit_price(line.price))
        elif line.price and line.price != price:
            status = PRICE_CHANGED
            reason = "Priced at %s, the order said %s" % (
                unit_price(price), unit_price(line.price))

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

# An order is its partner's number: purchase order numbers are unique per
# buyer, and two customers may both send a 4500000042. So everything below
# takes both, and there is no lookup by number alone except
# `orders_numbered`, which says how many there are.

def order_row(conn: sqlite3.Connection, po_number: str,
              partner_id: str) -> Optional[Dict[str, Any]]:
    return db.one(conn, "SELECT * FROM purchase_order WHERE partner = ?"
                        " AND po_number = ?", (partner_id, po_number))


def orders_numbered(conn: sqlite3.Connection, po_number: str) -> List[Dict[str, Any]]:
    """Every order with this number, whoever's: for a caller that has only it."""
    return db.rows(conn, "SELECT * FROM purchase_order WHERE po_number = ?"
                         " ORDER BY partner", (po_number,))


def order_lines(conn: sqlite3.Connection, po_number: str,
                partner_id: str) -> List[Dict[str, Any]]:
    return db.rows(conn,
                   "SELECT * FROM order_line WHERE partner = ? AND po_number = ?"
                   " ORDER BY CAST(line AS INTEGER), line", (partner_id, po_number))


def shipment_row(conn: sqlite3.Connection, shipment_id: str) -> Optional[Dict[str, Any]]:
    return db.one(conn, "SELECT * FROM shipment WHERE shipment_id = ?", (shipment_id,))


def latest_shipment(conn: sqlite3.Connection, po_number: str,
                    partner_id: str) -> Dict[str, Any]:
    return db.one(conn, "SELECT * FROM shipment WHERE partner = ? AND po_number = ?"
                        " ORDER BY rowid DESC LIMIT 1", (partner_id, po_number)) or {}


def uninvoiced_shipments(conn: sqlite3.Connection, po_number: str,
                         partner_id: str) -> List[Dict[str, Any]]:
    """Consignments no invoice names yet, oldest first."""
    return db.rows(conn,
                   "SELECT * FROM shipment WHERE partner = ? AND po_number = ?"
                   " AND shipment_id NOT IN (SELECT shipment_id FROM invoice)"
                   " ORDER BY rowid", (partner_id, po_number))


def consignment_lines(conn: sqlite3.Connection,
                      shipment_id: str) -> List[Dict[str, Any]]:
    """The order lines one consignment carried, with *its* quantities.

    `order_line.shipped` and `.invoiced` are running totals over every
    consignment. An 856 or an 810 for one consignment reports that
    consignment's share, so the rows come back with both fields set to it -
    which is what the writers read.

    A shipment packed before consignments were recorded line by line has no
    rows here; it was the order's only one, so its share is everything that
    shipped.
    """
    shipment = shipment_row(conn, shipment_id)
    if shipment is None:
        return []
    carried = {row["line"]: row["quantity"] for row in db.rows(
        conn, "SELECT line, quantity FROM shipment_line WHERE shipment_id = ?",
        (shipment_id,))}
    out = []
    for row in order_lines(conn, shipment["po_number"], shipment["partner"]):
        quantity = carried.get(row["line"]) if carried else (
            row["shipped"] if number(row["shipped"]) > 0 else None)
        if quantity is None:
            continue
        row = dict(row)
        row["shipped"] = row["invoiced"] = quantity
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# The documents that follow an accepted order
# ---------------------------------------------------------------------------

def create_shipment(conn: sqlite3.Connection, po_number: str, partner_id: str,
                    when: Optional[datetime.datetime] = None) -> Optional[Dict[str, Any]]:
    """Pack what has been confirmed and not yet shipped.

    Returns the shipment a despatch advice should name, which is not always a
    new one. An invoice that comes due before the despatch has to pack the
    goods so it has something to bill; when the despatch then arrives there is
    nothing left to pack, and packing it again would give the order two
    consignments, two bills of lading, and an 856 and an 810 naming different
    ones. So the existing shipment is returned instead.

    What is packed is the *difference* between confirmed and shipped, not
    everything confirmed, so a quantity raised or a line added after despatch
    ships as a second consignment of its own. Each consignment records what
    it carried in `shipment_line`.
    """
    moment = when or db.moment(conn)
    order = order_row(conn, po_number, partner_id)
    if order is None:
        return None
    lines = order_lines(conn, po_number, partner_id)

    if not any(number(row["confirmed"]) > 0 for row in lines):
        conn.execute("UPDATE purchase_order SET status = 'rejected'"
                     " WHERE partner = ? AND po_number = ?", (partner_id, po_number))
        conn.commit()
        return None

    shipping = [(row, number(row["confirmed"]) - number(row["shipped"]))
                for row in lines]
    shipping = [(row, delta) for row, delta in shipping if delta > 0]
    if not shipping:
        # Everything confirmed is already packed. Whoever asked wants the
        # consignment to name, not another one.
        return latest_shipment(conn, po_number, partner_id) or None

    if _behaviour(conn, partner_id) == "over-ship":
        shipping = [(row, over_shipped(delta)) for row, delta in shipping]

    units = sum(delta for _row, delta in shipping)
    shipment_id = "SHP%d" % db.next_number(conn, "shipment")
    for row, delta in shipping:
        conn.execute("UPDATE order_line SET shipped = ? WHERE partner = ?"
                     " AND po_number = ? AND line = ?",
                     (quantity_text(number(row["shipped"]) + delta),
                      partner_id, po_number, row["line"]))
        conn.execute("INSERT INTO shipment_line (shipment_id, line, quantity)"
                     " VALUES (?,?,?)", (shipment_id, row["line"],
                                         quantity_text(delta)))

    conn.execute(
        "INSERT INTO shipment (shipment_id, po_number, partner, shipped_on, carrier,"
        " scac, tracking, bol, cartons, weight, at, seq)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (shipment_id, po_number, order["partner"], local(moment).date().isoformat(),
         "United Parcel Service", "UPSN", _tracking(shipment_id),
         str(db.next_number(conn, "bol")),
         max(1, int(math.ceil(float(units) / UNITS_PER_CARTON))),
         quantity_text(units * 2), db.now(conn), db.next_seq(conn)))
    conn.execute("UPDATE purchase_order SET status = 'shipped'"
                 " WHERE partner = ? AND po_number = ?", (partner_id, po_number))
    conn.commit()
    return shipment_row(conn, shipment_id)


def over_shipped(quantity: Decimal) -> Decimal:
    """What an over-shipping seller packs where it should have packed `quantity`.

    Three in ten more, rounded up to a whole unit, and never less than one
    whole unit extra: 100 is 130, 10 is 13, and 1 is 2. A quantity that is
    not whole stays not whole when the one extra unit is what applies - half
    a kilogram is a kilogram and a half - since a weight ships in fractions
    and rounding it would bill for goods that were never packed. The
    acknowledgment said the
    ordered quantity, which is how a real over-shipment goes - the warehouse
    packed a full carton, and the buyer finds out from the ship notice and
    at the dock, not from a promise (#212).
    """
    more = (quantity * OVER_SHIP_FRACTION).to_integral_value(
        rounding=decimal.ROUND_CEILING)
    return max(more, quantity + 1)


def _behaviour(conn: sqlite3.Connection, partner_id: str) -> str:
    row = conn.execute("SELECT behaviour FROM partner WHERE id = ?",
                       (partner_id,)).fetchone()
    return row["behaviour"] if row else ""


def _tracking(shipment_id: str) -> str:
    """A tracking number derived from the shipment, so it is reproducible.

    UPS's 1Z format with a checkable-looking body; it is not a real number and
    the carrier will not know it, which is the intended behaviour for a mock.
    """
    digits = "".join(ch for ch in shipment_id if ch.isdigit()).rjust(9, "0")[-9:]
    return "1Z999AA1%s" % digits


def create_invoice(conn: sqlite3.Connection, po_number: str, partner_id: str,
                   shipment_id: str = "",
                   when: Optional[datetime.datetime] = None,
                   tax_rate: str = "0") -> Optional[Dict[str, Any]]:
    """Invoice one consignment, at the price the acknowledgment confirmed.

    One invoice per consignment, so each 810 names the one shipment it bills
    and a buyer can match every bill to a delivery. An order that shipped in
    two consignments is billed twice.
    """
    moment = when or db.moment(conn)
    order = order_row(conn, po_number, partner_id)
    if order is None:
        return None
    billable = consignment_lines(conn, shipment_id)
    if not billable:
        return None

    subtotal = Decimal("0.00")
    current = {row["line"]: row for row in order_lines(conn, po_number, partner_id)}
    for row in billable:
        billed = number(row["invoiced"])
        conn.execute("UPDATE order_line SET invoiced = ? WHERE partner = ?"
                     " AND po_number = ? AND line = ?",
                     (quantity_text(number(current[row["line"]]["invoiced"]) + billed),
                      partner_id, po_number, row["line"]))
        subtotal += cents(billed * number(row["price"], "0.00"))

    tax = cents(subtotal * Decimal(tax_rate))
    invoice_number = "INV%d" % db.next_number(conn, "invoice")
    conn.execute(
        "INSERT INTO invoice (invoice_number, po_number, partner, shipment_id,"
        " invoiced_on, currency, subtotal, tax, total, terms_days, discount_pct,"
        " discount_days, at, seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (invoice_number, po_number, order["partner"], shipment_id,
         local(moment).date().isoformat(), order["currency"], db.money(subtotal),
         db.money(tax), db.money(subtotal + tax), 30, "2", 10, db.now(conn),
         db.next_seq(conn)))
    billed_total = sum((number(row["total"], "0.00") for row in db.rows(
        conn, "SELECT total FROM invoice WHERE partner = ? AND po_number = ?",
        (partner_id, po_number))), Decimal("0.00"))
    conn.execute("UPDATE purchase_order SET status = 'invoiced', total = ?"
                 " WHERE partner = ? AND po_number = ?",
                 (db.money(billed_total), partner_id, po_number))
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
# Said to a partner that names a number another partner holds. Deliberately
# the *same* words as an unknown order: a customer has no business learning
# which numbers its competitors use.
NUMBER_IN_USE = "order number already in use"
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
    moment = when or db.moment(conn)
    # The partner's own order with that number, or none: another partner's
    # order with the same number is a different order, and not this one's to
    # change.
    order = order_row(conn, change.po_number, partner["id"])
    if order is None:
        return ChangeOutcome(change.po_number, REFUSED, NOT_FOUND)
    # A change request is about an order the partner placed *with the mock*,
    # never one the mock placed with it - however the partner's role got to
    # where it is. A supplier turned customer is still not the buyer of what
    # the mock bought from it. A restated 850 arrives here too, so this and
    # the pipeline's refusal of an 850 on a placed number hold together.
    if order["direction"] == PLACED:
        return ChangeOutcome(change.po_number, REFUSED, NOT_FOUND)
    if order["status"] == "invoiced":
        return ChangeOutcome(change.po_number, REFUSED, ALREADY_INVOICED)

    existing = {row["line"]: row for row in
                order_lines(conn, change.po_number, partner["id"])}

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
                " WHERE partner = ? AND po_number = ? AND line = ?",
                (REJECTED, "Order cancelled at the buyer's request",
                 partner["id"], change.po_number, row["line"]))
        conn.execute("UPDATE purchase_order SET status = 'cancelled', total = ?"
                     " WHERE partner = ? AND po_number = ?",
                     ("0.00", partner["id"], change.po_number))
        conn.commit()
        return ChangeOutcome(change.po_number, APPLIED, "Order cancelled",
                             cancelled=True)

    outcome = ChangeOutcome(change.po_number)
    for line in change.lines:
        row = existing.get(line.number)
        action = line.action or CHANGE_LINE

        if action == DELETE:
            outcome.lines.append(_delete_line(conn, partner, change, line, row))
            continue
        if action == NO_CHANGE and row is not None:
            outcome.lines.append({"line": line.number, "action": action,
                                  "status": row["status"], "reason": ""})
            continue
        if row is None or action == ADD:
            outcome.lines.append(_add_line(conn, partner, change, line, moment))
            continue
        outcome.lines.append(_change_line(conn, partner, change, line, row, moment))

    _retotal(conn, change.po_number, partner["id"])
    conn.commit()
    return outcome


def _delete_line(conn, partner, change, line, row) -> Dict[str, Any]:
    if row is None:
        return {"line": line.number, "action": "DI", "status": REJECTED,
                "reason": "there is no line %s to delete" % line.number}
    if number(row["shipped"]) > 0:
        return {"line": line.number, "action": "DI", "status": REJECTED,
                "reason": "%s, so line %s cannot be deleted"
                          % (ALREADY_SHIPPED, line.number)}
    conn.execute(
        "UPDATE order_line SET status = ?, confirmed = '0', reason = ?"
        " WHERE partner = ? AND po_number = ? AND line = ?",
        (REJECTED, "Line deleted at the buyer's request", partner["id"],
         change.po_number, line.number))
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
        "INSERT OR REPLACE INTO order_line (partner, po_number, line, sku, upc,"
        " description, quantity, uom, price, ordered_price, status, confirmed,"
        " shipped, invoiced, reason, scheduled_on)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (partner["id"], change.po_number, line.number, (item["sku"] if item else line.sku),
         line.upc or (item["upc"] if item else ""),
         line.description or (item["description"] if item else ""),
         quantity_text(line.quantity), line.uom, unit_price(price),
         unit_price(line.price), status, quantity_text(confirmed), "0", "0",
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
        " scheduled_on = ? WHERE partner = ? AND po_number = ? AND line = ?",
        (quantity_text(wanted), line.uom or row["uom"], unit_price(price),
         unit_price(line.price or number(row["ordered_price"], "0.00")), status,
         quantity_text(confirmed), reason, scheduled or row["scheduled_on"],
         partner["id"], change.po_number, line.number))
    return {"line": line.number, "action": action, "status": status,
            "reason": reason}


def _retotal(conn: sqlite3.Connection, po_number: str, partner_id: str) -> None:
    total = Decimal("0.00")
    for row in order_lines(conn, po_number, partner_id):
        total += cents(number(row["confirmed"]) * number(row["price"], "0.00"))
    status = "received" if total > 0 else "cancelled"
    current = order_row(conn, po_number, partner_id)
    if current and current["status"] in ("shipped", "invoiced"):
        status = current["status"]
    conn.execute("UPDATE purchase_order SET total = ?, status = ?"
                 " WHERE partner = ? AND po_number = ?",
                 (db.money(total), status, partner_id, po_number))
