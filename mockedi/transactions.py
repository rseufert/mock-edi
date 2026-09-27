"""Business documents in, business documents out.

This is the only module that knows both that an 850 keeps the purchase order
number in BEG03 and that an ORDERS keeps it in BGM's C106/1004.  Everything
above it works with `Order` and `Line`; everything below it works with
segments.

Readers are deliberately forgiving.  A real trading partner sends what its
software sends, and a mock that only accepted its own output would be useless
for the job it exists to do - catching the difference between what you send
and what the other side expects.  So a reader takes the item number from
whichever qualifier is present, accepts the requested delivery date under any
of the three qualifiers that mean it, and treats a missing line number as "the
position it arrived in".

Writers are not forgiving: they emit one profile, documented in the README, so
that what comes out is stable enough to assert on.

Six documents each way:

| Read                   | Write                   | X12 | EDIFACT |
| ---------------------- | ----------------------- | --- | ------- |
| `read_order`           | `write_order`           | 850 | ORDERS  |
| `read_change`          | `write_change`          | 860 | ORDCHG  |
| `read_response`        | `write_response`        | 855 | ORDRSP  |
| `read_despatch`        | `write_despatch`        | 856 | DESADV  |
| `read_invoice`         | `write_invoice`         | 810 | INVOIC  |
| `read_change_response` | `write_change_response` | 865 | ORDRSP  |

The mock is the seller, so it reads the first two and writes the last four.
The other direction of each exists too, for the buyer the mock is becoming:
nothing in the pipeline calls `write_order`, `write_change` or the four
readers yet, and a buyer needs all six. Each pair is held to the other -
`ReadersInvertTheWriters` and `tests/test_order_writers.py` - and to
everybody else by `tests/test_readers.py`.

The one asymmetry worth knowing about is party roles. A seller writes itself
as `SU`/`SE` and its partner as `BY`; `write_order` and `write_change` are
the other way round, because the mock is the customer there. A document with
those swapped validates perfectly and names the wrong company, so the flip
lives in `_buyer_parties_x12` and `_buyer_parties_edifact` where it can be
seen, rather than in a parameter to the seller's helpers.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import schema
from .envelope import Message, Seg, seg, parse_date

# The item number qualifiers a reader will take a SKU from, best first.
SKU_QUALIFIERS = ("VP", "SA", "BP", "IN", "SK", "MG", "MF")
UPC_QUALIFIERS = ("UP", "EN", "UI")
# The date qualifiers that all mean "when the buyer wants it".
REQUESTED_X12 = ("002", "010", "038", "068", "017")
REQUESTED_EDIFACT = ("2", "17", "10")


@dataclass
class Party:
    role: str = ""
    name: str = ""
    identifier: str = ""
    street: str = ""
    city: str = ""
    region: str = ""
    postal: str = ""
    country: str = ""


@dataclass
class Line:
    number: str = ""
    sku: str = ""
    upc: str = ""
    description: str = ""
    quantity: Decimal = Decimal("0")
    uom: str = "EA"
    price: Decimal = Decimal("0.00")

    @property
    def amount(self) -> Decimal:
        return (self.quantity * self.price).quantize(Decimal("0.01"))


# The line-level verbs of a change request, in the vocabulary the X12 670
# element uses. The EDIFACT reader translates 1229 into these so that
# `documents.apply_change` has one set of words to reason about.
ADD = "AI"
CHANGE_LINE = "CA"
DELETE = "DI"
NO_CHANGE = "NC"
PRICE_CHANGE = "PC"
QUANTITY_DOWN = "QD"
QUANTITY_UP = "QI"

EDIFACT_ACTION_TO_CHANGE = {"1": ADD, "2": DELETE, "3": CHANGE_LINE,
                            "4": NO_CHANGE, "": CHANGE_LINE}
CHANGE_TO_EDIFACT_ACTION = {ADD: "1", DELETE: "2", CHANGE_LINE: "3",
                            NO_CHANGE: "4", PRICE_CHANGE: "3",
                            QUANTITY_DOWN: "3", QUANTITY_UP: "3"}

# The line-level verdict a seller gives, in ACK01's vocabulary (X12 element
# 668). EDIFACT has no equivalent element: an ORDRSP says the same thing by
# how much it confirms, so the reader works these out from the quantities -
# which is `acknowledgment_type` run backwards.
ACCEPTED = "IA"
REJECTED = "IR"
SHORT = "IQ"
BACKORDERED = "IB"
RESCHEDULED = "DR"

# BEG01 / BGM 1225 values that mean "cancel the whole thing".
CANCEL_PURPOSES = ("01", "03")
# BEG01 values that mean an 850 is restating an order rather than placing one.
CHANGE_PURPOSES = ("01", "03", "04", "05")


@dataclass
class ChangeLine(Line):
    """One line of a change request: a line, and what to do to it."""
    action: str = CHANGE_LINE


@dataclass
class Change:
    """A change to an order that has already been sent."""
    po_number: str = ""
    purpose: str = "04"
    sequence: str = ""
    changed_on: Optional[datetime.date] = None
    ordered_on: Optional[datetime.date] = None
    currency: str = ""
    lines: List[ChangeLine] = field(default_factory=list)

    @property
    def cancels(self) -> bool:
        """Whether this asks for the whole order to be withdrawn."""
        return self.purpose in CANCEL_PURPOSES


@dataclass
class Order:
    """A purchase order, whichever dialect carried it."""
    po_number: str = ""
    ordered_on: Optional[datetime.date] = None
    requested_on: Optional[datetime.date] = None
    currency: str = "USD"
    purpose: str = "00"
    parties: Dict[str, Party] = field(default_factory=dict)
    lines: List[Line] = field(default_factory=list)

    @property
    def ship_to(self) -> Party:
        for role in ("ST", "DP", "BY", "CN"):
            if role in self.parties:
                return self.parties[role]
        return Party()

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal("0.00"))



@dataclass
class ResponseLine(Line):
    """One line of a seller's answer: what was ordered, and what was promised."""
    confirmed: Decimal = Decimal("0")
    status: str = ACCEPTED
    scheduled_on: Optional[datetime.date] = None
    reason: str = ""
    action: str = ""        # only an 865, or an ORDRSP answering an ORDCHG

    @property
    def short_by(self) -> Decimal:
        """How much of this line the seller did not commit to."""
        return self.quantity - self.confirmed


@dataclass
class Response:
    """A seller's answer to an order: an 855, or an ORDRSP."""
    po_number: str = ""
    seller_order: str = ""
    verdict: str = ""       # BAK02, or BGM's 4343
    responded_on: Optional[datetime.date] = None
    ordered_on: Optional[datetime.date] = None
    currency: str = ""
    sequence: str = ""      # the change this answers, for an 865
    lines: List[ResponseLine] = field(default_factory=list)

    @property
    def confirmed_total(self) -> Decimal:
        return sum((line.confirmed * line.price for line in self.lines),
                   Decimal("0.00"))

    @property
    def rejected(self) -> List[ResponseLine]:
        return [line for line in self.lines if line.status == REJECTED]

    @property
    def short(self) -> List[ResponseLine]:
        return [line for line in self.lines
                if line.status != REJECTED and line.short_by > 0]


@dataclass
class DespatchItem:
    """One item in a consignment."""
    line: str = ""
    sku: str = ""
    upc: str = ""
    quantity: Decimal = Decimal("0")
    uom: str = "EA"
    ordered: Decimal = Decimal("0")


@dataclass
class Despatch:
    """What a seller says it has shipped: an 856, or a DESADV."""
    shipment_id: str = ""
    shipped_on: Optional[datetime.date] = None
    po_number: str = ""
    ordered_on: Optional[datetime.date] = None
    carrier: str = ""
    scac: str = ""
    tracking: str = ""
    bol: str = ""
    cartons: int = 0
    weight: Decimal = Decimal("0")
    items: List[DespatchItem] = field(default_factory=list)

    @property
    def total_quantity(self) -> Decimal:
        return sum((item.quantity for item in self.items), Decimal("0"))


@dataclass
class InvoiceLine(Line):
    """One billed line. `quantity` is what was invoiced, not what was ordered."""
    amount_stated: Optional[Decimal] = None

    @property
    def amount(self) -> Decimal:
        """What the line comes to - as stated, when the sender stated it.

        EDIFACT names the line amount in MOA+203; X12 leaves it to be worked
        out from the quantity and the price. A reader that always multiplied
        would hide a supplier whose arithmetic disagrees with its own prices,
        which is one of the things a three-way match is looking for.
        """
        if self.amount_stated is not None:
            return self.amount_stated
        return (self.quantity * self.price).quantize(Decimal("0.01"))


@dataclass
class Invoice:
    """A seller's bill: an 810, or an INVOIC."""
    invoice_number: str = ""
    invoiced_on: Optional[datetime.date] = None
    po_number: str = ""
    ordered_on: Optional[datetime.date] = None
    shipment_id: str = ""
    bol: str = ""
    seller_order: str = ""
    currency: str = ""
    subtotal: Optional[Decimal] = None
    tax: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    terms_days: int = 0
    discount_pct: Decimal = Decimal("0")
    discount_days: int = 0
    lines: List[InvoiceLine] = field(default_factory=list)

    @property
    def line_total(self) -> Decimal:
        """What the lines add up to, whatever the stated total says.

        These disagreeing is a finding rather than an error: it is the most
        common thing wrong with a real invoice.
        """
        return sum((line.amount for line in self.lines), Decimal("0.00"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def group_by(segments: Sequence[Seg], trigger: str,
             stop: Sequence[str] = ()) -> Iterator[List[Seg]]:
    """Split a flat segment list into the repetitions of one loop.

    Enough for reading: a loop starts at its trigger and runs to the next
    trigger or to a segment that can only belong outside it.  Validation is
    what checks the structure properly; this only has to find the lines.
    """
    stoppers = set(stop)
    current: Optional[List[Seg]] = None
    for item in segments:
        if item.tag == trigger:
            if current:
                yield current
            current = [item]
        elif current is not None:
            if item.tag in stoppers:
                yield current
                current = None
            else:
                current.append(item)
    if current:
        yield current


def number(value: str, default: str = "0") -> Decimal:
    try:
        return Decimal((value or default).strip() or default)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def quantity_text(value: Decimal) -> str:
    """A quantity without a pointless `.00`, which is how senders write them."""
    if value == value.to_integral_value():
        return str(int(value))
    return str(value.normalize())


def price_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def implied_decimal(value: Decimal) -> str:
    """TDS01 and its relatives carry an integer with two implied decimals.

    125.00 goes on the wire as `12500`.  Every EDI integration meets this once,
    usually as an invoice a hundred times too large.
    """
    return str(int((value * 100).to_integral_value()))


def date_text(value: Optional[datetime.date]) -> str:
    return value.strftime("%Y%m%d") if value else ""


def _ids(item: Seg, start: int, count: int = 5) -> Dict[str, str]:
    """The qualifier/identifier pairs that end PO1, IT1 and LIN."""
    out: Dict[str, str] = {}
    for index in range(count):
        qualifier = item.get(start + index * 2)
        identifier = item.get(start + index * 2 + 1)
        if qualifier and identifier:
            out.setdefault(qualifier, identifier)
    return out


def _pick(ids: Dict[str, str], qualifiers: Sequence[str]) -> str:
    for qualifier in qualifiers:
        if ids.get(qualifier):
            return ids[qualifier]
    return ""


# ---------------------------------------------------------------------------
# Reading an order
# ---------------------------------------------------------------------------

def read_order(message: Message, dialect: str) -> Order:
    return _read_order_x12(message) if dialect == "X12" else _read_order_edifact(message)


def _read_order_x12(message: Message) -> Order:
    order = Order()
    body = message.body
    beg = message.find("BEG")
    if beg is not None:
        order.purpose = beg.get(1) or "00"
        order.po_number = beg.get(3)
        order.ordered_on = parse_date(beg.get(5))
    cur = message.find("CUR")
    if cur is not None and cur.get(2):
        order.currency = cur.get(2)

    for item in body:
        if item.tag == "DTM" and item.get(1) in REQUESTED_X12 and not order.requested_on:
            order.requested_on = parse_date(item.get(2))
        if item.tag == "PO1":
            break

    header = []
    for item in body:
        if item.tag == "PO1":
            break
        header.append(item)
    for block in group_by(header, "N1"):
        party = _party_x12(block)
        order.parties.setdefault(party.role, party)

    detail = body[len(header):]
    for index, block in enumerate(group_by(detail, "PO1", stop=("CTT", "SE")), start=1):
        order.lines.append(_line_x12(block, index))
    return order


def _party_x12(block: Sequence[Seg]) -> Party:
    head = block[0]
    party = Party(role=head.get(1), name=head.get(2), identifier=head.get(4))
    for item in block[1:]:
        if item.tag == "N3":
            party.street = " ".join(p for p in (item.get(1), item.get(2)) if p)
        elif item.tag == "N4":
            party.city, party.region = item.get(1), item.get(2)
            party.postal, party.country = item.get(3), item.get(4) or "US"
    return party


def _line_x12(block: Sequence[Seg], fallback: int) -> Line:
    head = block[0]
    ids = _ids(head, 6)
    line = Line(
        number=head.get(1) or str(fallback),
        sku=_pick(ids, SKU_QUALIFIERS),
        upc=_pick(ids, UPC_QUALIFIERS),
        quantity=number(head.get(2)),
        uom=head.get(3) or "EA",
        price=number(head.get(4), "0.00"),
    )
    for item in block[1:]:
        if item.tag == "PID" and item.get(5):
            line.description = line.description or item.get(5)
    if not line.sku:
        # Some buyers identify items by a qualifier the mock does not list.
        # Take whatever is there rather than refuse the line - but never the
        # UPC, which has its own field and is not a part number.
        spare = [value for qualifier, value in sorted(ids.items())
                 if qualifier not in UPC_QUALIFIERS]
        if spare:
            line.sku = spare[0]
    return line


def _read_order_edifact(message: Message) -> Order:
    order = Order()
    body = message.body
    bgm = message.find("BGM")
    if bgm is not None:
        order.po_number = bgm.comp(2, 1)
        order.purpose = bgm.get(3) or "9"

    header: List[Seg] = []
    for item in body:
        if item.tag == "LIN":
            break
        header.append(item)

    for item in header:
        if item.tag == "DTM":
            qualifier, value = item.comp(1, 1), item.comp(1, 2)
            if qualifier == "137" and not order.ordered_on:
                order.ordered_on = parse_date(value)
            elif qualifier in REQUESTED_EDIFACT and not order.requested_on:
                order.requested_on = parse_date(value)
        elif item.tag == "CUX" and item.comp(1, 2):
            order.currency = item.comp(1, 2)
        elif item.tag == "NAD":
            party = _party_edifact(item)
            order.parties.setdefault(party.role, party)

    detail = body[len(header):]
    for index, block in enumerate(group_by(detail, "LIN", stop=("UNS", "UNT")), start=1):
        order.lines.append(_line_edifact(block, index))
    return order


def _party_edifact(item: Seg) -> Party:
    return Party(
        role=item.get(1), identifier=item.comp(2, 1),
        name=item.comp(4, 1) or item.comp(3, 1),
        street=item.comp(5, 1), city=item.get(6), region=item.get(7),
        postal=item.get(8), country=item.get(9) or "DE",
    )


def _line_edifact(block: Sequence[Seg], fallback: int) -> Line:
    head = block[0]
    line = Line(number=head.get(1) or str(fallback))
    identifier, kind = head.comp(3, 1), head.comp(3, 2)
    if kind in UPC_QUALIFIERS:
        line.upc = identifier
    else:
        line.sku = identifier

    for item in block[1:]:
        if item.tag == "PIA":
            value, kind = item.comp(2, 1), item.comp(2, 2)
            if kind in UPC_QUALIFIERS:
                line.upc = line.upc or value
            elif not line.sku:
                line.sku = value
        elif item.tag == "QTY":
            if item.comp(1, 1) in ("21", "1"):
                line.quantity = number(item.comp(1, 2))
                line.uom = schema.UOM_FROM_EDIFACT.get(item.comp(1, 3), item.comp(1, 3) or "EA")
        elif item.tag == "PRI":
            if item.comp(1, 1) in ("AAA", "AAB", "AAE"):
                line.price = number(item.comp(1, 2), "0.00")
        elif item.tag == "IMD":
            line.description = line.description or item.comp(3, 4)
    return line


# ---------------------------------------------------------------------------
# Writing the answers
#
# One profile, documented, so that tests can assert on it.  Where a standard
# allows several ways of saying something - and EDIFACT nearly always does -
# the choice is noted where it is made.
# ---------------------------------------------------------------------------

def _iso(value: str) -> str:
    """An ISO date from the database as CCYYMMDD."""
    text = (value or "").strip()
    return text.replace("-", "")[:8] if text else ""


def _line_rows(lines: Sequence[Dict], field: str) -> List[Dict]:
    """The lines that carry a non-zero quantity in `field` - what actually ships."""
    return [row for row in lines if number(str(row.get(field) or "0")) > 0]


def acknowledgment_type(lines: Sequence[Dict], dialect: str) -> str:
    """The overall verdict code, derived from what happened to the lines."""
    statuses = {row.get("status") or ACCEPTED for row in lines}
    if statuses == {REJECTED}:
        return "RJ" if dialect == "X12" else "RE"
    if statuses <= {ACCEPTED}:
        return "AD" if dialect == "X12" else "AP"
    return "AC"


def write_response(dialect: str, us: Party, partner: Dict, order: Dict,
                   lines: Sequence[Dict], when: datetime.datetime) -> List[Seg]:
    builder = _x12_855 if dialect == "X12" else _edifact_ordrsp
    return builder(us, partner, order, lines, when)


def write_despatch(dialect: str, us: Party, partner: Dict, order: Dict,
                   lines: Sequence[Dict], shipment: Dict,
                   when: datetime.datetime) -> List[Seg]:
    builder = _x12_856 if dialect == "X12" else _edifact_desadv
    return builder(us, partner, order, lines, shipment, when)


def write_invoice(dialect: str, us: Party, partner: Dict, order: Dict,
                  lines: Sequence[Dict], invoice: Dict, shipment: Dict,
                  when: datetime.datetime) -> List[Seg]:
    builder = _x12_810 if dialect == "X12" else _edifact_invoic
    return builder(us, partner, order, lines, invoice, shipment, when)


# -- X12

def _x12_parties(us: Party, order: Dict, roles: Sequence[Tuple[str, str]]) -> List[Seg]:
    out: List[Seg] = []
    for role, source in roles:
        if source == "us":
            out.append(seg("N1", role, us.name, "92", us.identifier))
            if us.street:
                out.append(seg("N3", us.street))
                out.append(seg("N4", us.city, us.region, us.postal, us.country))
        else:
            out.append(seg("N1", role, order.get("ship_to_name") or "", "92",
                           order.get("ship_to_id") or ""))
            if order.get("ship_to_street"):
                out.append(seg("N3", order["ship_to_street"]))
                out.append(seg("N4", order.get("ship_to_city") or "",
                               order.get("ship_to_region") or "",
                               order.get("ship_to_postal") or "",
                               order.get("ship_to_country") or "US"))
    return out


def _x12_855(us: Party, partner: Dict, order: Dict, lines: Sequence[Dict],
             when: datetime.datetime) -> List[Seg]:
    out: List[Seg] = [seg(
        "BAK", "00", acknowledgment_type(lines, "X12"), order["po_number"],
        _iso(order.get("ordered_on")), "", "", "",
        order.get("seller_order") or "",      # BAK08: the seller's order
        when.strftime("%Y%m%d"))]              # BAK09: acknowledged on
    out.append(seg("REF", "VN", order.get("seller_order") or ""))
    out.append(seg("DTM", "137", when.strftime("%Y%m%d")))
    if order.get("currency"):
        out.insert(1, seg("CUR", "SE", order["currency"]))
    out.extend(_x12_parties(us, order, (("SE", "us"), ("ST", "order"))))

    for row in lines:
        out.append(seg("PO1", row["line"], quantity_text(number(row["quantity"])),
                       row["uom"], price_text(number(row["price"], "0.00")), "",
                       "VP", row["sku"], *(("UP", row["upc"]) if row.get("upc") else ())))
        status = row.get("status") or ACCEPTED
        confirmed = quantity_text(number(str(row.get("confirmed") or "0")))
        # ACK04/05 carry the date the seller is committing to; an outright
        # rejection commits to nothing, so they are left empty.
        if status == REJECTED:
            out.append(seg("ACK", status, "0", row["uom"]))
        else:
            out.append(seg("ACK", status, confirmed, row["uom"], "068",
                           _iso(row.get("scheduled_on"))))
        if row.get("description"):
            out.append(seg("PID", "F", "", "", "", row["description"]))
        if row.get("reason"):
            out.append(seg("REF", "ZZ", "", row["reason"]))
    out.append(seg("CTT", str(len(lines))))
    return out


def _x12_856(us: Party, partner: Dict, order: Dict, lines: Sequence[Dict],
             shipment: Dict, when: datetime.datetime) -> List[Seg]:
    shipped = _line_rows(lines, "shipped")
    out: List[Seg] = [seg(
        "BSN", "00", shipment["shipment_id"], when.strftime("%Y%m%d"),
        when.strftime("%H%M"),
        # 0004 is Shipment, Order, Item - the levels this notice actually uses.
        "0004")]

    # Level 1: the shipment.
    out.append(seg("HL", "1", "", "S", "1"))
    out.append(seg("TD1", "CTN25", str(shipment.get("cartons") or 0), "", "", "",
                   "G", str(shipment.get("weight") or 0), "LB"))
    out.append(seg("TD5", "", "2", shipment.get("scac") or "", "M",
                   shipment.get("carrier") or ""))
    out.append(seg("REF", "BM", shipment.get("bol") or ""))
    if shipment.get("tracking"):
        out.append(seg("REF", "CN", shipment["tracking"]))
    out.append(seg("DTM", "011", _iso(shipment.get("shipped_on"))))
    out.extend(_x12_parties(us, order, (("SF", "us"), ("ST", "order"))))

    # Level 2: the order this shipment is against.
    out.append(seg("HL", "2", "1", "O", "1"))
    out.append(seg("PRF", order["po_number"], "", "", _iso(order.get("ordered_on"))))

    # Level 3 and beyond: one per shipped item.
    node = 3
    for row in shipped:
        out.append(seg("HL", str(node), "2", "I", "0"))
        out.append(seg("LIN", row["line"], "VP", row["sku"],
                       *(("UP", row["upc"]) if row.get("upc") else ())))
        out.append(seg("SN1", row["line"],
                       quantity_text(number(str(row["shipped"]))), row["uom"], "",
                       quantity_text(number(row["quantity"])), row["uom"]))
        node += 1
    # CTT01 in an 856 counts HL segments, not line items.
    out.append(seg("CTT", str(node - 1)))
    return out


def _x12_810(us: Party, partner: Dict, order: Dict, lines: Sequence[Dict],
             invoice: Dict, shipment: Dict, when: datetime.datetime) -> List[Seg]:
    billed = _line_rows(lines, "invoiced")
    out: List[Seg] = [seg(
        "BIG", _iso(invoice.get("invoiced_on")), invoice["invoice_number"],
        _iso(order.get("ordered_on")), order["po_number"], "", "", "DI", "00")]
    out.append(seg("CUR", "SE", invoice.get("currency") or "USD"))
    out.append(seg("REF", "VN", order.get("seller_order") or ""))
    if shipment:
        out.append(seg("REF", "BM", shipment.get("bol") or ""))
        out.append(seg("REF", "SI", shipment.get("shipment_id") or ""))
    out.extend(_x12_parties(us, order, (("RE", "us"), ("ST", "order"))))
    out.append(seg("N1", "BT", partner.get("name") or "", "92", partner.get("id") or ""))

    # ITD08 is the discount amount, which only exists if a discount is offered.
    discount_pct = number(str(invoice.get("discount_pct") or "0"))
    if discount_pct > 0:
        out.append(seg("ITD", "08", "3", str(discount_pct), "",
                       str(invoice.get("discount_days") or 0), "",
                       str(invoice.get("terms_days") or 30)))
    else:
        out.append(seg("ITD", "01", "3", "", "", "", "",
                       str(invoice.get("terms_days") or 30)))
    if shipment.get("shipped_on"):
        out.append(seg("DTM", "011", _iso(shipment["shipped_on"])))

    for row in billed:
        out.append(seg("IT1", row["line"],
                       quantity_text(number(str(row["invoiced"]))), row["uom"],
                       price_text(number(row["price"], "0.00")), "",
                       "VP", row["sku"], *(("UP", row["upc"]) if row.get("upc") else ())))
        if row.get("description"):
            out.append(seg("PID", "F", "", "", "", row["description"]))

    out.append(seg("TDS", implied_decimal(number(invoice["total"], "0.00"))))
    tax = number(str(invoice.get("tax") or "0"), "0.00")
    if tax > 0:
        out.append(seg("TXI", "ST", price_text(tax)))
    out.append(seg("CTT", str(len(billed))))
    return out


# -- EDIFACT
#
# Where the standard admits several conventions, this is the profile the mock
# writes.  The line-level verdict in an ORDRSP is the one worth stating out
# loud, because trading partners genuinely differ: BGM's 4343 carries the
# overall response, and per line the confirmed quantity goes in QTY+113
# ("quantity to be delivered") beside the ordered QTY+21, with the shortfall
# in QTY+83 and the reason in FTX+AAO.  A rejected line is QTY+113:0.

def _edifact_unit(uom: str) -> str:
    return schema.UOM_TO_EDIFACT.get(uom, uom or "PCE")


def _nad(role: str, identifier: str, name: str = "", street: str = "",
         city: str = "", region: str = "", postal: str = "",
         country: str = "") -> Seg:
    return seg("NAD", role, [identifier, "", "92"], "", [name] if name else "",
               [street] if street else "", city, region, postal, country)


def _edifact_parties(us: Party, partner: Dict, order: Dict,
                     roles: Sequence[str]) -> List[Seg]:
    out: List[Seg] = []
    for role in roles:
        if role == "SU":
            out.append(_nad("SU", us.identifier, us.name, us.street, us.city,
                            us.region, us.postal, us.country))
        elif role == "BY":
            out.append(_nad("BY", partner.get("id") or "", partner.get("name") or "",
                            partner.get("street") or "", partner.get("city") or "",
                            partner.get("region") or "", partner.get("postal") or "",
                            partner.get("country") or ""))
        elif role == "DP":
            out.append(_nad("DP", order.get("ship_to_id") or "",
                            order.get("ship_to_name") or "",
                            order.get("ship_to_street") or "",
                            order.get("ship_to_city") or "",
                            order.get("ship_to_region") or "",
                            order.get("ship_to_postal") or "",
                            order.get("ship_to_country") or ""))
    return out


def _edifact_ordrsp(us: Party, partner: Dict, order: Dict, lines: Sequence[Dict],
                    when: datetime.datetime) -> List[Seg]:
    out: List[Seg] = [seg(
        "BGM", ["231"], [order.get("seller_order") or order["po_number"]], "9",
        acknowledgment_type(lines, "EDIFACT"))]
    out.append(seg("DTM", ["137", when.strftime("%Y%m%d"), "102"]))
    out.append(seg("RFF", ["ON", order["po_number"]]))
    if order.get("ordered_on"):
        out.append(seg("DTM", ["4", _iso(order["ordered_on"]), "102"]))
    out.extend(_edifact_parties(us, partner, order, ("SU", "BY", "DP")))
    out.append(seg("CUX", ["2", order.get("currency") or "EUR", "9"]))

    for row in lines:
        unit = _edifact_unit(row["uom"])
        out.append(seg("LIN", row["line"], "", [row["sku"], "VP"]))
        if row.get("upc"):
            out.append(seg("PIA", "1", [row["upc"], "UP"]))
        if row.get("description"):
            out.append(seg("IMD", "F", "", ["", "", "", row["description"]]))
        ordered = number(row["quantity"])
        confirmed = number(str(row.get("confirmed") or "0"))
        out.append(seg("QTY", ["21", quantity_text(ordered), unit]))
        out.append(seg("QTY", ["113", quantity_text(confirmed), unit]))
        if confirmed < ordered:
            out.append(seg("QTY", ["83", quantity_text(ordered - confirmed), unit]))
        if row.get("scheduled_on") and confirmed > 0:
            out.append(seg("DTM", ["2", _iso(row["scheduled_on"]), "102"]))
        out.append(seg("PRI", ["AAA", price_text(number(row["price"], "0.00"))]))
        if row.get("reason"):
            out.append(seg("FTX", "AAO", "", "", [row["reason"]]))

    out.append(seg("UNS", "S"))
    out.append(seg("CNT", ["2", str(len(lines))]))
    return out


def _edifact_desadv(us: Party, partner: Dict, order: Dict, lines: Sequence[Dict],
                    shipment: Dict, when: datetime.datetime) -> List[Seg]:
    shipped = _line_rows(lines, "shipped")
    out: List[Seg] = [seg("BGM", ["351"], [shipment["shipment_id"]], "9")]
    out.append(seg("DTM", ["137", when.strftime("%Y%m%d"), "102"]))
    out.append(seg("DTM", ["11", _iso(shipment.get("shipped_on")), "102"]))
    out.append(seg("RFF", ["ON", order["po_number"]]))
    if shipment.get("tracking"):
        # The tracking number goes in a header RFF, not in TDT02: 8028 is
        # seventeen characters and a parcel tracking number is often longer.
        out.append(seg("RFF", ["CN", shipment["tracking"]]))
    out.extend(_edifact_parties(us, partner, order, ("SU", "DP")))
    out.append(seg("TDT", "20", shipment.get("bol") or "", "", "",
                   [shipment.get("scac") or "", "", "", shipment.get("carrier") or ""]))
    # CPS is the despatch advice's hierarchy: one consignment, packed flat.
    out.append(seg("CPS", "1"))
    out.append(seg("PAC", str(shipment.get("cartons") or 0), "", ["CT", "", "", "Carton"]))

    for row in shipped:
        unit = _edifact_unit(row["uom"])
        out.append(seg("LIN", row["line"], "", [row["sku"], "VP"]))
        if row.get("upc"):
            out.append(seg("PIA", "1", [row["upc"], "UP"]))
        out.append(seg("QTY", ["12", quantity_text(number(str(row["shipped"]))), unit]))
        out.append(seg("RFF", ["ON", order["po_number"], row["line"]]))
    return out


def _edifact_invoic(us: Party, partner: Dict, order: Dict, lines: Sequence[Dict],
                    invoice: Dict, shipment: Dict,
                    when: datetime.datetime) -> List[Seg]:
    billed = _line_rows(lines, "invoiced")
    currency = invoice.get("currency") or "EUR"
    out: List[Seg] = [seg("BGM", ["380"], [invoice["invoice_number"]], "9")]
    out.append(seg("DTM", ["137", _iso(invoice.get("invoiced_on")), "102"]))
    if shipment.get("shipped_on"):
        out.append(seg("DTM", ["35", _iso(shipment["shipped_on"]), "102"]))
    out.append(seg("RFF", ["ON", order["po_number"]]))
    if shipment.get("shipment_id"):
        out.append(seg("RFF", ["AAK", shipment["shipment_id"]]))
    out.extend(_edifact_parties(us, partner, order, ("SU", "BY", "DP")))
    out.append(seg("CUX", ["2", currency, "4"]))
    out.append(seg("PAT", "3", "", ["5", "3", "D", str(invoice.get("terms_days") or 30)]))

    for row in billed:
        unit = _edifact_unit(row["uom"])
        invoiced = number(str(row["invoiced"]))
        price = number(row["price"], "0.00")
        out.append(seg("LIN", row["line"], "", [row["sku"], "VP"]))
        if row.get("upc"):
            out.append(seg("PIA", "1", [row["upc"], "UP"]))
        if row.get("description"):
            out.append(seg("IMD", "F", "", ["", "", "", row["description"]]))
        out.append(seg("QTY", ["47", quantity_text(invoiced), unit]))
        out.append(seg("MOA", ["203", price_text(invoiced * price)]))
        out.append(seg("PRI", ["AAA", price_text(price)]))

    out.append(seg("UNS", "S"))
    # Every EDIFACT total is named; X12 puts the same numbers in fixed
    # positions of TDS and leaves the reader to know which is which.
    out.append(seg("MOA", ["79", price_text(number(invoice["subtotal"], "0.00"))]))
    out.append(seg("MOA", ["124", price_text(number(str(invoice.get("tax") or "0"), "0.00"))]))
    out.append(seg("MOA", ["139", price_text(number(invoice["total"], "0.00"))]))
    out.append(seg("CNT", ["2", str(len(billed))]))
    return out


# ---------------------------------------------------------------------------
# Reading a change request
#
# X12 has a transaction set of its own for this (the 860, with BCH and POC);
# EDIFACT reuses the order message with a different document code and an
# action verb per line. Both read into the same `Change`.
# ---------------------------------------------------------------------------

def read_change(message: Message, dialect: str) -> Change:
    return (_read_change_x12(message) if dialect == "X12"
            else _read_change_edifact(message))


def _read_change_x12(message: Message) -> Change:
    change = Change()
    bch = message.find("BCH")
    if bch is not None:
        change.purpose = bch.get(1) or "04"
        change.po_number = bch.get(3)
        change.sequence = bch.get(5)
        change.changed_on = parse_date(bch.get(6))
        change.ordered_on = parse_date(bch.get(10))
    cur = message.find("CUR")
    if cur is not None and cur.get(2):
        change.currency = cur.get(2)

    for index, block in enumerate(group_by(message.body, "POC",
                                           stop=("CTT", "SE")), start=1):
        head = block[0]
        ids = _ids(head, 8)
        line = ChangeLine(
            number=head.get(1) or str(index),
            action=head.get(2) or CHANGE_LINE,
            sku=_pick(ids, SKU_QUALIFIERS),
            upc=_pick(ids, UPC_QUALIFIERS),
            quantity=number(head.get(3)),
            uom=head.get(5) or "EA",
            price=number(head.get(6), "0.00"),
        )
        for item in block[1:]:
            if item.tag == "PID" and item.get(5):
                line.description = line.description or item.get(5)
        change.lines.append(line)
    return change


def _read_change_edifact(message: Message) -> Change:
    change = Change()
    bgm = message.find("BGM")
    if bgm is not None:
        change.po_number = bgm.comp(2, 1)
        # BGM's message function code says whether this is a change or a
        # cancellation; 1 is cancellation in both dialects' vocabulary.
        change.purpose = "01" if bgm.get(3) == "1" else "04"
        change.sequence = bgm.comp(2, 3)

    header: List[Seg] = []
    for item in message.body:
        if item.tag == "LIN":
            break
        header.append(item)
    for item in header:
        if item.tag == "DTM" and item.comp(1, 1) == "137":
            change.changed_on = parse_date(item.comp(1, 2))
        elif item.tag == "CUX" and item.comp(1, 2):
            change.currency = item.comp(1, 2)
        elif item.tag == "RFF" and item.comp(1, 1) == "ON":
            change.po_number = change.po_number or item.comp(1, 2)

    detail = message.body[len(header):]
    for index, block in enumerate(group_by(detail, "LIN", stop=("UNS", "UNT")),
                                  start=1):
        base = _line_edifact(block, index)
        line = ChangeLine(
            number=base.number, sku=base.sku, upc=base.upc,
            description=base.description, quantity=base.quantity,
            uom=base.uom, price=base.price,
            action=EDIFACT_ACTION_TO_CHANGE.get(block[0].get(2), CHANGE_LINE),
        )
        change.lines.append(line)
    return change


def change_from_order(order: Order, held: Sequence[str] = ()) -> Change:
    """An 850 sent with a change purpose, read as the change it is.

    A buyer may restate a whole order rather than send an 860, and `BEG01`
    says so with `04` (Change) or `05` (Replace).  Every line is a change to
    the line of the same number, and a line in `held` - the numbers the
    order used to have - that the restatement no longer mentions has been
    deleted.

    For `05` that reading is the only one. For `04` it is a choice, and the
    mock makes the same one: an 850 has no line-level change codes, so a
    restated order can say "drop this line" only by leaving it out. A
    partner that sends `04` with just the lines it is changing needs an 860
    instead.
    """
    change = Change(po_number=order.po_number, purpose=order.purpose,
                    changed_on=order.ordered_on, ordered_on=order.ordered_on,
                    currency=order.currency)
    for line in order.lines:
        change.lines.append(ChangeLine(
            number=line.number, sku=line.sku, upc=line.upc,
            description=line.description, quantity=line.quantity,
            uom=line.uom, price=line.price, action=CHANGE_LINE))
    mentioned = {line.number for line in order.lines}
    for number in held:
        if number not in mentioned:
            change.lines.append(ChangeLine(number=number, action=DELETE))
    return change


# ---------------------------------------------------------------------------
# Writing the answer to a change request
# ---------------------------------------------------------------------------

def write_change_response(dialect: str, us: Party, partner: Dict, order: Dict,
                          lines: Sequence[Dict], change: Change,
                          when: datetime.datetime) -> List[Seg]:
    builder = _x12_865 if dialect == "X12" else _edifact_ordrsp_change
    return builder(us, partner, order, lines, change, when)


def _x12_865(us: Party, partner: Dict, order: Dict, lines: Sequence[Dict],
             change: Change, when: datetime.datetime) -> List[Seg]:
    out: List[Seg] = [seg(
        "BCA", "00", acknowledgment_type(lines, "X12"), order["po_number"],
        "", change.sequence, when.strftime("%Y%m%d"), "", "", "",
        _iso(order.get("ordered_on")))]        # BCA10: the purchase order date
    out.append(seg("CUR", "SE", order.get("currency") or "USD"))
    out.append(seg("REF", "VN", order.get("seller_order") or ""))
    out.append(seg("DTM", "137", when.strftime("%Y%m%d")))
    out.extend(_x12_parties(us, order, (("SE", "us"), ("ST", "order"))))

    for row in lines:
        out.append(seg("POC", row["line"], row.get("change_action") or CHANGE_LINE,
                       quantity_text(number(row["quantity"])), "", row["uom"],
                       price_text(number(row["price"], "0.00")), "",
                       "VP", row["sku"],
                       *(("UP", row["upc"]) if row.get("upc") else ())))
        status = row.get("status") or ACCEPTED
        confirmed = quantity_text(number(str(row.get("confirmed") or "0")))
        if status == REJECTED:
            out.append(seg("ACK", status, "0", row["uom"]))
        else:
            out.append(seg("ACK", status, confirmed, row["uom"], "068",
                           _iso(row.get("scheduled_on"))))
        if row.get("description"):
            out.append(seg("PID", "F", "", "", "", row["description"]))
        if row.get("reason"):
            out.append(seg("REF", "ZZ", "", row["reason"]))
    out.append(seg("CTT", str(len(lines))))
    return out


def _edifact_ordrsp_change(us: Party, partner: Dict, order: Dict,
                           lines: Sequence[Dict], change: Change,
                           when: datetime.datetime) -> List[Seg]:
    """An ORDRSP answering an ORDCHG.

    EDIFACT has no separate change acknowledgment message, so the response to
    a change is the same message that answers an order - with each line
    carrying the action it was given, so the buyer can tell which of its
    requested changes were taken.
    """
    body = _edifact_ordrsp(us, partner, order, lines, when)
    actions = {row["line"]: CHANGE_TO_EDIFACT_ACTION.get(
        row.get("change_action") or CHANGE_LINE, "3") for row in lines}
    for item in body:
        if item.tag == "LIN" and item.get(1) in actions:
            item.elements[1] = actions[item.get(1)]
    return body



# ---------------------------------------------------------------------------
# Writing an order, and a change to one
#
# The other four writers above are the seller's: the mock answers an order it
# was sent.  These two are the buyer's, for the partner the mock buys from -
# and the difference that matters is not the segments, it is which side of
# every party role the mock is on.  A seller writes itself as SU and its
# partner as BY; here it is the other way round, and getting that backwards
# produces a document that validates perfectly and names the wrong company as
# the customer.
#
# Nothing in the mock calls these yet; the pipeline that will is #125's own
# wiring. They are pure, and `read_order` and `read_change` are the test.
# ---------------------------------------------------------------------------

def write_order(dialect: str, us: Party, partner: Dict, order: Dict,
                lines: Sequence[Dict], when: datetime.datetime) -> List[Seg]:
    """An 850 or an ORDERS: what the mock is ordering, and from whom."""
    builder = _x12_850 if dialect == "X12" else _edifact_orders
    return builder(us, partner, order, lines, when)


def write_change(dialect: str, us: Party, partner: Dict, order: Dict,
                 change: Change, when: datetime.datetime) -> List[Seg]:
    """An 860 or an ORDCHG: what the mock wants changed about an order."""
    builder = _x12_860 if dialect == "X12" else _edifact_ordchg
    return builder(us, partner, order, change, when)


def _buyer_parties_x12(us: Party, partner: Dict, order: Dict) -> List[Seg]:
    """BY is the mock, SE the supplier, ST wherever the goods are to go.

    The mirror of `_x12_parties`, which writes the mock as the seller. Kept
    separate rather than bent into that one, because the flip is the thing a
    reader of this file needs to see.
    """
    out: List[Seg] = [seg("N1", "BY", us.name, "92", us.identifier)]
    if us.street:
        out.append(seg("N3", us.street))
        out.append(seg("N4", us.city, us.region, us.postal, us.country))
    out.append(seg("N1", "SE", partner.get("name") or "", "92",
                   partner.get("id") or ""))
    if partner.get("street"):
        out.append(seg("N3", partner["street"]))
        out.append(seg("N4", partner.get("city") or "",
                       partner.get("region") or "",
                       partner.get("postal") or "",
                       partner.get("country") or "US"))
    # Where it is to be delivered. A buyer that names no other address is
    # ordering to itself, which is the common case and worth writing out
    # rather than leaving the supplier to assume.
    out.append(seg("N1", "ST", order.get("ship_to_name") or us.name, "92",
                   order.get("ship_to_id") or us.identifier))
    street = order.get("ship_to_street") or us.street
    if street:
        out.append(seg("N3", street))
        out.append(seg("N4", order.get("ship_to_city") or us.city,
                       order.get("ship_to_region") or us.region,
                       order.get("ship_to_postal") or us.postal,
                       order.get("ship_to_country") or us.country or "US"))
    return out


def _order_line_x12(row: Dict) -> List[Seg]:
    out = [seg("PO1", row["line"], quantity_text(number(row["quantity"])),
               row["uom"], price_text(number(row.get("price"), "0.00")), "",
               "VP", row["sku"],
               *(("UP", row["upc"]) if row.get("upc") else ()))]
    if row.get("description"):
        out.append(seg("PID", "F", "", "", "", row["description"]))
    # A real 850 often dates each line too. Not written here: `Line` has no
    # field for a line-level date, so nothing would read it back and no
    # round-trip could check it. Give `Line` the field first.
    return out


def _x12_850(us: Party, partner: Dict, order: Dict, lines: Sequence[Dict],
             when: datetime.datetime) -> List[Seg]:
    out: List[Seg] = [seg(
        "BEG", order.get("purpose") or "00", "SA", order["po_number"], "",
        _iso(order.get("ordered_on")) or when.strftime("%Y%m%d"))]
    out.append(seg("CUR", "BY", order.get("currency") or "USD"))
    if order.get("requested_on"):
        out.append(seg("DTM", "002", _iso(order["requested_on"])))
    out.extend(_buyer_parties_x12(us, partner, order))
    for row in lines:
        out.extend(_order_line_x12(row))
    out.append(seg("CTT", str(len(lines))))
    return out


def _x12_860(us: Party, partner: Dict, order: Dict, change: Change,
             when: datetime.datetime) -> List[Seg]:
    out: List[Seg] = [seg(
        "BCH", change.purpose or "04", "SA", change.po_number or order["po_number"],
        "", change.sequence or "1",
        date_text(change.changed_on) or when.strftime("%Y%m%d"),
        "", "", "",
        # BCH10 is the date of the order being changed, not of the change.
        date_text(change.ordered_on) or _iso(order.get("ordered_on")))]
    out.append(seg("CUR", "BY", change.currency or order.get("currency") or "USD"))
    for line in change.lines:
        out.append(seg("POC", line.number, line.action,
                       quantity_text(line.quantity), "", line.uom,
                       price_text(line.price), "",
                       "VP", line.sku,
                       *(("UP", line.upc) if line.upc else ())))
        if line.description:
            out.append(seg("PID", "F", "", "", "", line.description))
    out.append(seg("CTT", str(len(change.lines))))
    return out


def _buyer_parties_edifact(us: Party, partner: Dict, order: Dict) -> List[Seg]:
    """BY is the mock, SU the supplier, DP where the goods are to go."""
    out = [_nad("BY", us.identifier, us.name, us.street, us.city, us.region,
                us.postal, us.country),
           _nad("SU", partner.get("id") or "", partner.get("name") or "",
                partner.get("street") or "", partner.get("city") or "",
                partner.get("region") or "", partner.get("postal") or "",
                partner.get("country") or "")]
    out.append(_nad("DP", order.get("ship_to_id") or us.identifier,
                    order.get("ship_to_name") or us.name,
                    order.get("ship_to_street") or us.street,
                    order.get("ship_to_city") or us.city,
                    order.get("ship_to_region") or us.region,
                    order.get("ship_to_postal") or us.postal,
                    order.get("ship_to_country") or us.country))
    return out


def _edifact_order_line(row: Dict) -> List[Seg]:
    unit = _edifact_unit(row["uom"])
    out = [seg("LIN", row["line"], "", [row["sku"], "VP"])]
    if row.get("upc"):
        out.append(seg("PIA", "1", [row["upc"], "UP"]))
    if row.get("description"):
        out.append(seg("IMD", "F", "", ["", "", "", row["description"]]))
    out.append(seg("QTY", ["21", quantity_text(number(row["quantity"])), unit]))
    out.append(seg("PRI", ["AAA", price_text(number(row.get("price"), "0.00"))]))
    return out


def _edifact_orders(us: Party, partner: Dict, order: Dict,
                    lines: Sequence[Dict], when: datetime.datetime) -> List[Seg]:
    out: List[Seg] = [seg("BGM", ["220"], [order["po_number"]], "9")]
    out.append(seg("DTM", ["137", _iso(order.get("ordered_on"))
                           or when.strftime("%Y%m%d"), "102"]))
    if order.get("requested_on"):
        out.append(seg("DTM", ["2", _iso(order["requested_on"]), "102"]))
    out.extend(_buyer_parties_edifact(us, partner, order))
    out.append(seg("CUX", ["2", order.get("currency") or "EUR", "9"]))
    for row in lines:
        out.extend(_edifact_order_line(row))
    out.append(seg("UNS", "S"))
    out.append(seg("CNT", ["2", str(len(lines))]))
    return out


def _edifact_ordchg(us: Party, partner: Dict, order: Dict, change: Change,
                    when: datetime.datetime) -> List[Seg]:
    """An ORDCHG.

    EDIFACT says "change" and "cancel" in BGM's 1225 rather than in a purpose
    code of its own, and carries the change's sequence as C106's third
    component - which is what `_read_change_edifact` reads it back out of.
    """
    po_number = change.po_number or order["po_number"]
    # 230 is "Purchase order change request" in 1001, which is what an ORDCHG
    # is; 1225 then says whether this one changes the order or withdraws it.
    # 4 rather than 5: "change" is what a buyer amending some lines means, and
    # "replace" would tell the supplier to read the message as the whole order.
    out: List[Seg] = [seg(
        "BGM", ["230"], [po_number, "", change.sequence or "1"],
        "1" if change.cancels else "4")]
    out.append(seg("DTM", ["137", date_text(change.changed_on)
                           or when.strftime("%Y%m%d"), "102"]))
    out.append(seg("RFF", ["ON", po_number]))
    out.extend(_buyer_parties_edifact(us, partner, order))
    out.append(seg("CUX", ["2", change.currency or order.get("currency")
                           or "EUR", "9"]))
    for line in change.lines:
        unit = _edifact_unit(line.uom)
        out.append(seg("LIN", line.number,
                       CHANGE_TO_EDIFACT_ACTION.get(line.action, "3"),
                       [line.sku, "VP"]))
        if line.upc:
            out.append(seg("PIA", "1", [line.upc, "UP"]))
        if line.description:
            out.append(seg("IMD", "F", "", ["", "", "", line.description]))
        out.append(seg("QTY", ["21", quantity_text(line.quantity), unit]))
        out.append(seg("PRI", ["AAA", price_text(line.price)]))
    out.append(seg("UNS", "S"))
    out.append(seg("CNT", ["2", str(len(change.lines))]))
    return out

# ---------------------------------------------------------------------------
# Reading the answers
#
# The other direction of every writer above.  Nothing in the mock calls these
# yet - it is the seller, and these read what a seller sends - but a buyer
# needs them, and they are testable on their own: the strongest test of a
# reader is that it gets back what the writer beside it was given.
#
# Forgiving in the same way `read_order` is.  A supplier's translator will put
# the item number under a qualifier this module does not list, leave out every
# optional segment, number its lines from nothing, and state an amount that
# disagrees with its own price - and a reader that refused any of that would
# be no use for finding out.
# ---------------------------------------------------------------------------

def read_response(message: Message, dialect: str) -> Response:
    """An 855 or an ORDRSP: what the seller committed to."""
    return (_read_response_x12(message) if dialect == "X12"
            else _read_response_edifact(message))


def read_change_response(message: Message, dialect: str) -> Response:
    """An 865, or an ORDRSP answering an ORDCHG.

    The same shape as a response, plus which change it answers and what the
    seller did with each requested action.
    """
    return (_read_change_response_x12(message) if dialect == "X12"
            else _read_response_edifact(message))


def read_despatch(message: Message, dialect: str) -> Despatch:
    """An 856 or a DESADV: what is on its way."""
    return (_read_despatch_x12(message) if dialect == "X12"
            else _read_despatch_edifact(message))


def read_invoice(message: Message, dialect: str) -> Invoice:
    """An 810 or an INVOIC: what is being billed."""
    return (_read_invoice_x12(message) if dialect == "X12"
            else _read_invoice_edifact(message))


# -- X12

def _header_x12(message: Message, trigger: str) -> List[Seg]:
    """Everything before the first detail segment."""
    out = []
    for item in message.body:
        if item.tag == trigger:
            break
        out.append(item)
    return out


def _ack_x12(block: Sequence[Seg], ordered: Decimal, uom: str):
    """The ACK segment of a PO1 or POC loop, as a verdict and a quantity.

    A rejected line is written with no date and no quantity worth having, so
    its confirmed quantity is zero however the sender spelled it.
    """
    for item in block[1:]:
        if item.tag != "ACK":
            continue
        status = item.get(1) or ACCEPTED
        if status == REJECTED:
            return status, Decimal("0"), None
        scheduled = None
        # ACK04 is the date qualifier and ACK05 the date; some senders send
        # the date alone, so a date in either position is read.
        for position in (5, 4):
            scheduled = parse_date(item.get(position))
            if scheduled:
                break
        return status, number(item.get(2)), scheduled
    # No ACK at all: the line is confirmed as ordered, which is what a
    # sender leaving it out means.
    return ACCEPTED, ordered, None


def _reason_x12(block: Sequence[Seg]) -> str:
    for item in block[1:]:
        if item.tag == "REF" and item.get(1) == "ZZ":
            return item.get(3) or item.get(2)
    return ""


def _description_x12(block: Sequence[Seg]) -> str:
    for item in block[1:]:
        if item.tag == "PID" and item.get(5):
            return item.get(5)
    return ""


def _response_line_x12(block: Sequence[Seg], fallback: int,
                       ids_from: int) -> ResponseLine:
    head = block[0]
    ids = _ids(head, ids_from)
    quantity_at, uom_at, price_at = ((3, 5, 6) if head.tag == "POC"
                                     else (2, 3, 4))
    ordered = number(head.get(quantity_at))
    uom = head.get(uom_at) or "EA"
    status, confirmed, scheduled = _ack_x12(block, ordered, uom)
    line = ResponseLine(
        number=head.get(1) or str(fallback),
        sku=_pick(ids, SKU_QUALIFIERS) or _spare_sku(ids),
        upc=_pick(ids, UPC_QUALIFIERS),
        description=_description_x12(block),
        quantity=ordered,
        uom=uom,
        price=number(head.get(price_at), "0.00"),
        confirmed=confirmed,
        status=status,
        scheduled_on=scheduled,
        reason=_reason_x12(block),
    )
    if head.tag == "POC":
        line.action = head.get(2) or CHANGE_LINE
    return line


def _read_response_x12(message: Message) -> Response:
    response = Response()
    bak = message.find("BAK")
    if bak is not None:
        response.verdict = bak.get(2)
        response.po_number = bak.get(3)
        response.ordered_on = parse_date(bak.get(4))
        response.seller_order = bak.get(8)
        response.responded_on = parse_date(bak.get(9))
    _finish_response_x12(message, response, "PO1", 6)
    return response


def _read_change_response_x12(message: Message) -> Response:
    response = Response()
    bca = message.find("BCA")
    if bca is not None:
        response.verdict = bca.get(2)
        response.po_number = bca.get(3)
        response.sequence = bca.get(5)
        response.responded_on = parse_date(bca.get(6))
        response.ordered_on = parse_date(bca.get(10))
    _finish_response_x12(message, response, "POC", 8)
    return response


def _finish_response_x12(message: Message, response: Response, trigger: str,
                         ids_from: int) -> None:
    """The parts an 855 and an 865 read the same way."""
    cur = message.find("CUR")
    if cur is not None and cur.get(2):
        response.currency = cur.get(2)
    header = _header_x12(message, trigger)
    if not response.seller_order:
        for item in header:
            if item.tag == "REF" and item.get(1) == "VN":
                response.seller_order = item.get(2)
                break
    if response.responded_on is None:
        for item in header:
            if item.tag == "DTM" and item.get(1) == "137":
                response.responded_on = parse_date(item.get(2))
                break
    detail = message.body[len(header):]
    for index, block in enumerate(
            group_by(detail, trigger, stop=("CTT", "SE")), start=1):
        response.lines.append(_response_line_x12(block, index, ids_from))


def _from_implied(value: str) -> Decimal:
    """The inverse of `implied_decimal`: `12500` is 125.00.

    Reading TDS01 as a plain number is the invoice a hundred times too large
    that every EDI integration meets once.
    """
    return (number(value) / Decimal("100")).quantize(Decimal("0.01"))


def _spare_sku(ids: Dict[str, str]) -> str:
    """Any identifier that is not a UPC, for a qualifier we do not list."""
    spare = [value for qualifier, value in sorted(ids.items())
             if qualifier not in UPC_QUALIFIERS]
    return spare[0] if spare else ""


def _read_despatch_x12(message: Message) -> Despatch:
    despatch = Despatch()
    bsn = message.find("BSN")
    if bsn is not None:
        despatch.shipment_id = bsn.get(2)
        despatch.shipped_on = parse_date(bsn.get(3))

    # The HL tree, by parent pointer rather than by position: a real 856 puts
    # pack and tare levels between the order and the item, and a reader that
    # assumed shipment/order/item in that order would lose the items under
    # them.
    nodes = _hl_nodes(message)
    for node in nodes.values():
        if node["level"] == "S":
            _despatch_shipment_x12(node["segments"], despatch)
        elif node["level"] == "O":
            _despatch_order_x12(node["segments"], despatch)

    for node in nodes.values():
        if node["level"] != "I" or not _under(nodes, node, "O"):
            continue
        item = _despatch_item_x12(node["segments"])
        if item is not None:
            despatch.items.append(item)
    return despatch


def _hl_nodes(message: Message) -> Dict[str, Dict[str, Any]]:
    """The HL hierarchy: each node's level, parent, and the segments under it."""
    nodes: Dict[str, Dict[str, Any]] = {}
    current = None
    for item in message.body:
        if item.tag == "HL":
            current = {"id": item.get(1), "parent": item.get(2),
                       "level": item.get(3), "segments": []}
            nodes[item.get(1)] = current
        elif current is not None:
            current["segments"].append(item)
    return nodes


def _under(nodes: Dict[str, Dict[str, Any]], node: Dict[str, Any],
           level: str) -> bool:
    """Whether any ancestor of this node is at `level`."""
    seen = set()
    parent = nodes.get(node["parent"])
    while parent is not None and parent["id"] not in seen:
        if parent["level"] == level:
            return True
        seen.add(parent["id"])
        parent = nodes.get(parent["parent"])
    return False


def _despatch_shipment_x12(segments: Sequence[Seg], despatch: Despatch) -> None:
    for item in segments:
        if item.tag == "TD1":
            # TD102 is the lading quantity and TD107 the weight; TD108 is the
            # unit it is in, which the model does not keep because every
            # sender the mock has met sends pounds or kilos and says so there.
            despatch.cartons = int(number(item.get(2)))
            despatch.weight = number(item.get(7))
        elif item.tag == "TD5":
            despatch.scac = item.get(3)
            despatch.carrier = item.get(5)
        elif item.tag == "REF" and item.get(1) == "BM":
            despatch.bol = item.get(2)
        elif item.tag == "REF" and item.get(1) == "CN":
            despatch.tracking = item.get(2)
        elif item.tag == "DTM" and item.get(1) in ("011", "017"):
            despatch.shipped_on = parse_date(item.get(2)) or despatch.shipped_on


def _despatch_order_x12(segments: Sequence[Seg], despatch: Despatch) -> None:
    for item in segments:
        if item.tag == "PRF":
            despatch.po_number = item.get(1) or despatch.po_number
            despatch.ordered_on = parse_date(item.get(4)) or despatch.ordered_on


def _despatch_item_x12(segments: Sequence[Seg]) -> Optional[DespatchItem]:
    lin = next((item for item in segments if item.tag == "LIN"), None)
    sn1 = next((item for item in segments if item.tag == "SN1"), None)
    if lin is None and sn1 is None:
        return None
    ids = _ids(lin, 2) if lin is not None else {}
    item = DespatchItem(
        line=(lin.get(1) if lin is not None else "") or
             (sn1.get(1) if sn1 is not None else ""),
        sku=_pick(ids, SKU_QUALIFIERS) or _spare_sku(ids),
        upc=_pick(ids, UPC_QUALIFIERS),
    )
    if sn1 is not None:
        item.quantity = number(sn1.get(2))
        item.uom = sn1.get(3) or "EA"
        item.ordered = number(sn1.get(5))
    return item


def _read_invoice_x12(message: Message) -> Invoice:
    invoice = Invoice()
    big = message.find("BIG")
    if big is not None:
        invoice.invoiced_on = parse_date(big.get(1))
        invoice.invoice_number = big.get(2)
        invoice.ordered_on = parse_date(big.get(3))
        invoice.po_number = big.get(4)
    cur = message.find("CUR")
    if cur is not None and cur.get(2):
        invoice.currency = cur.get(2)

    header = _header_x12(message, "IT1")
    for item in header:
        if item.tag == "REF" and item.get(1) == "VN":
            invoice.seller_order = invoice.seller_order or item.get(2)
        elif item.tag == "REF" and item.get(1) == "BM":
            invoice.bol = invoice.bol or item.get(2)
        elif item.tag == "REF" and item.get(1) == "SI":
            invoice.shipment_id = invoice.shipment_id or item.get(2)
        elif item.tag == "ITD":
            invoice.terms_days = int(number(item.get(7)))
            invoice.discount_pct = number(item.get(3))
            invoice.discount_days = int(number(item.get(5)))

    detail = message.body[len(header):]
    for index, block in enumerate(
            group_by(detail, "IT1", stop=("TDS", "CTT", "SE")), start=1):
        head = block[0]
        ids = _ids(head, 6)
        invoice.lines.append(InvoiceLine(
            number=head.get(1) or str(index),
            sku=_pick(ids, SKU_QUALIFIERS) or _spare_sku(ids),
            upc=_pick(ids, UPC_QUALIFIERS),
            description=_description_x12(block),
            quantity=number(head.get(2)),
            uom=head.get(3) or "EA",
            price=number(head.get(4), "0.00"),
        ))

    tds = message.find("TDS")
    if tds is not None:
        # TDS carries an integer with two implied decimals. Reading it as a
        # plain number is the invoice a hundred times too large that every
        # integration meets once.
        invoice.total = _from_implied(tds.get(1))
        if tds.get(2):
            invoice.subtotal = _from_implied(tds.get(2))
    for item in message.body:
        if item.tag == "TXI" and item.get(2):
            invoice.tax += number(item.get(2), "0.00")
    return invoice


# -- EDIFACT

def _edifact_qty(block: Sequence[Seg], code: str) -> Optional[Decimal]:
    for item in block:
        if item.tag == "QTY" and item.comp(1, 1) == code:
            return number(item.comp(1, 2))
    return None


def _edifact_moa(segments: Sequence[Seg], code: str) -> Optional[Decimal]:
    for item in segments:
        if item.tag == "MOA" and item.comp(1, 1) == code:
            return number(item.comp(1, 2), "0.00")
    return None


def _edifact_rff(segments: Sequence[Seg], code: str) -> Seg:
    for item in segments:
        if item.tag == "RFF" and item.comp(1, 1) == code:
            return item
    return None


def _edifact_dtm(segments: Sequence[Seg], codes: Sequence[str]):
    for code in codes:
        for item in segments:
            if item.tag == "DTM" and item.comp(1, 1) == code:
                found = parse_date(item.comp(1, 2))
                if found:
                    return found
    return None


def _edifact_ids(block: Sequence[Seg]) -> Dict[str, str]:
    """Every item number in a LIN loop: LIN's own C212, and every PIA's."""
    ids: Dict[str, str] = {}
    head = block[0]
    if head.comp(3, 1):
        ids.setdefault(head.comp(3, 2) or "VP", head.comp(3, 1))
    for item in block[1:]:
        if item.tag != "PIA":
            continue
        for position in range(2, 7):
            value, qualifier = item.comp(position, 1), item.comp(position, 2)
            if value:
                ids.setdefault(qualifier or "VP", value)
    return ids


def _edifact_description(block: Sequence[Seg]) -> str:
    for item in block[1:]:
        if item.tag == "IMD" and item.comp(3, 4):
            return item.comp(3, 4)
    return ""


def _edifact_unit_back(code: str) -> str:
    """An X12 unit from the UN/ECE code an EDIFACT document carries.

    `schema.UOM_FROM_EDIFACT` rather than a reverse search of the map going
    the other way: that one is many-to-one - both CA and CS are CT - so
    searching it backwards would give whichever came first. A code the mock
    does not know is passed through, because a unit it cannot translate is
    still what the sender said.
    """
    return schema.UOM_FROM_EDIFACT.get(code, code or "EA")


def _read_response_edifact(message: Message) -> Response:
    response = Response()
    body = message.body
    bgm = message.find("BGM")
    if bgm is not None:
        response.seller_order = bgm.comp(2, 1)
        response.verdict = bgm.get(4)
    header = _edifact_header(body)
    order_reference = _edifact_rff(header, "ON")
    if order_reference is not None:
        response.po_number = order_reference.comp(1, 2)
    change_reference = _edifact_rff(header, "CR")
    if change_reference is not None:
        response.sequence = change_reference.comp(1, 2)
    response.responded_on = _edifact_dtm(header, ("137",))
    response.ordered_on = _edifact_dtm(header, ("4", "171"))
    cux = message.find("CUX")
    if cux is not None:
        response.currency = cux.comp(1, 2)

    for index, block in enumerate(
            group_by(body[len(header):], "LIN", stop=("UNS", "CNT", "UNT")),
            start=1):
        head = block[0]
        ids = _edifact_ids(block)
        ordered = _edifact_qty(block, "21")
        confirmed = _edifact_qty(block, "113")
        if confirmed is None:
            confirmed = _edifact_qty(block, "12")
        if ordered is None:
            ordered = confirmed if confirmed is not None else Decimal("0")
        if confirmed is None:
            confirmed = ordered
        unit = ""
        for item in block:
            if item.tag == "QTY" and item.comp(1, 3):
                unit = item.comp(1, 3)
                break
        price = Decimal("0.00")
        for item in block[1:]:
            if item.tag == "PRI" and item.comp(1, 2):
                price = number(item.comp(1, 2), "0.00")
                break
        reason = ""
        for item in block[1:]:
            if item.tag == "FTX" and item.get(1) == "AAO":
                reason = item.comp(4, 1) or ""
                break
        line = ResponseLine(
            number=head.get(1) or str(index),
            sku=_pick(ids, SKU_QUALIFIERS) or _spare_sku(ids),
            upc=_pick(ids, UPC_QUALIFIERS),
            description=_edifact_description(block),
            quantity=ordered,
            uom=_edifact_unit_back(unit),
            price=price,
            confirmed=confirmed,
            # No element carries the verdict: an ORDRSP says it by how much it
            # confirms, so this is `acknowledgment_type` run backwards.
            status=_verdict(ordered, confirmed),
            scheduled_on=_edifact_dtm(block[1:], ("2", "67")),
            reason=reason,
            action=EDIFACT_ACTION_TO_CHANGE.get(head.get(2), "")
                   if head.get(2) else "",
        )
        response.lines.append(line)
    return response


def _verdict(ordered: Decimal, confirmed: Decimal) -> str:
    if confirmed <= 0:
        return REJECTED
    if confirmed < ordered:
        return SHORT
    return ACCEPTED


def _edifact_header(body: Sequence[Seg]) -> List[Seg]:
    out = []
    for item in body:
        if item.tag == "LIN":
            break
        out.append(item)
    return out


def _read_despatch_edifact(message: Message) -> Despatch:
    despatch = Despatch()
    body = message.body
    bgm = message.find("BGM")
    if bgm is not None:
        despatch.shipment_id = bgm.comp(2, 1)
    header = _edifact_header(body)
    despatch.shipped_on = _edifact_dtm(header, ("11", "17", "137"))
    order_reference = _edifact_rff(header, "ON")
    if order_reference is not None:
        despatch.po_number = order_reference.comp(1, 2)
    tracking = _edifact_rff(header, "CN")
    if tracking is not None:
        despatch.tracking = tracking.comp(1, 2)
    for item in header:
        if item.tag == "TDT":
            despatch.bol = item.get(2)
            despatch.scac = item.comp(5, 1)
            despatch.carrier = item.comp(5, 4)
        elif item.tag == "PAC" and item.get(1):
            despatch.cartons = int(number(item.get(1)))

    for index, block in enumerate(
            group_by(body[len(header):], "LIN", stop=("UNS", "CNT", "UNT")),
            start=1):
        head = block[0]
        ids = _edifact_ids(block)
        shipped = _edifact_qty(block, "12")
        if shipped is None:
            shipped = _edifact_qty(block, "113") or Decimal("0")
        unit = ""
        for item in block:
            if item.tag == "QTY" and item.comp(1, 3):
                unit = item.comp(1, 3)
                break
        line = head.get(1) or str(index)
        reference = _edifact_rff(block[1:], "ON")
        if reference is not None and reference.comp(1, 3):
            # RFF+ON carries the order line in 1156, which is the buyer's own
            # line number and better than the position in the despatch.
            line = reference.comp(1, 3)
        despatch.items.append(DespatchItem(
            line=line,
            sku=_pick(ids, SKU_QUALIFIERS) or _spare_sku(ids),
            upc=_pick(ids, UPC_QUALIFIERS),
            quantity=shipped,
            uom=_edifact_unit_back(unit),
        ))
    return despatch


def _read_invoice_edifact(message: Message) -> Invoice:
    invoice = Invoice()
    body = message.body
    bgm = message.find("BGM")
    if bgm is not None:
        invoice.invoice_number = bgm.comp(2, 1)
    header = _edifact_header(body)
    invoice.invoiced_on = _edifact_dtm(header, ("137", "3"))
    order_reference = _edifact_rff(header, "ON")
    if order_reference is not None:
        invoice.po_number = order_reference.comp(1, 2)
    despatch_reference = _edifact_rff(header, "AAK")
    if despatch_reference is not None:
        invoice.shipment_id = despatch_reference.comp(1, 2)
    cux = message.find("CUX")
    if cux is not None:
        invoice.currency = cux.comp(1, 2)
    for item in header:
        if item.tag == "PAT" and item.comp(3, 4):
            invoice.terms_days = int(number(item.comp(3, 4)))

    for index, block in enumerate(
            group_by(body[len(header):], "LIN", stop=("UNS", "CNT", "UNT")),
            start=1):
        head = block[0]
        ids = _edifact_ids(block)
        billed = _edifact_qty(block, "47")
        if billed is None:
            billed = _edifact_qty(block, "12") or Decimal("0")
        unit = ""
        for item in block:
            if item.tag == "QTY" and item.comp(1, 3):
                unit = item.comp(1, 3)
                break
        price = Decimal("0.00")
        for item in block[1:]:
            if item.tag == "PRI" and item.comp(1, 2):
                price = number(item.comp(1, 2), "0.00")
                break
        invoice.lines.append(InvoiceLine(
            number=head.get(1) or str(index),
            sku=_pick(ids, SKU_QUALIFIERS) or _spare_sku(ids),
            upc=_pick(ids, UPC_QUALIFIERS),
            description=_edifact_description(block),
            quantity=billed,
            uom=_edifact_unit_back(unit),
            price=price,
            amount_stated=_edifact_moa(block[1:], "203"),
        ))

    summary = body[len(header):]
    # The totals are in the summary section, after UNS. Named, unlike X12's.
    tail = []
    seen_uns = False
    for item in summary:
        if item.tag == "UNS":
            seen_uns = True
        elif seen_uns:
            tail.append(item)
    invoice.subtotal = _edifact_moa(tail, "79")
    invoice.tax = _edifact_moa(tail, "124") or Decimal("0.00")
    invoice.total = _edifact_moa(tail, "139") or Decimal("0.00")
    return invoice
