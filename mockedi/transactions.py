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
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

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

ACCEPTED = "IA"
REJECTED = "IR"
SHORT = "IQ"
BACKORDERED = "IB"
RESCHEDULED = "DR"


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


def change_from_order(order: Order) -> Change:
    """An 850 sent with a change purpose, read as the change it is.

    A buyer may restate a whole order rather than send an 860, and `BEG01`
    says so with `04`.  Every line is a change to the line of the same number,
    and a line the order no longer mentions has been deleted - which the
    caller works out, because only it knows what the order used to hold.
    """
    change = Change(po_number=order.po_number, purpose=order.purpose,
                    changed_on=order.ordered_on, ordered_on=order.ordered_on,
                    currency=order.currency)
    for line in order.lines:
        change.lines.append(ChangeLine(
            number=line.number, sku=line.sku, upc=line.upc,
            description=line.description, quantity=line.quantity,
            uom=line.uom, price=line.price, action=CHANGE_LINE))
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
