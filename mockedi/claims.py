"""What a supplier says about an order the mock placed, and where it disagrees.

The mock as buyer (#116) receives a supplier's 855, 856, 810 and 865 and files
them under the order they name (#125). This module is what it then does with
them: it keeps each line of each document as a *claim*, derives the order's
running totals from the claims, and compares what the document says against
what the mock asked for.

A disagreement is a business finding, not a syntax finding. It never enters a
997 or a CONTRL and never changes whether a set is accepted: a buyer who
receives a well-formed 856 shipping 100 against a confirmed 90 acknowledges it
and takes the dispute elsewhere. That is the rule that decides the design, and
the reason `BusinessFinding` is kept on a list of its own that nothing deciding
`accepted` reads.

Claims are kept per document rather than as one number per line because a
supplier sends two 856s for two consignments, corrects an 855, or bills twice,
and a finding has to name the document it came from. `order_line.confirmed`,
`shipped` and `invoiced` on a placed order are derived from them: the latest
answer for a line is what is confirmed, and every consignment and every bill
adds to what was shipped and billed.

Each rule is one comparison, names both numbers, and is one function in the
table for its kind of document. The 855's rules (#126) and the 810's (#152)
are here; the 856's are #151.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from . import db, documents, schema
from .transactions import (REJECTED, number, price_text, quantity_text)
from .validate import BusinessFinding

# 668: the item is accepted, with a different item substituted for it.
SUBSTITUTED = "IS"
# 668: accepted, price changed. The supplier admits the change; the buyer
# still has to be told of it.
PRICE_CHANGED = "IP"
ACCEPTED_AS_ORDERED = "IA"

# What a document answering an order is, for deriving what was confirmed: the
# latest of these for a line is the line's confirmation.
ANSWERS = (schema.RESPONSE, schema.CHANGE_RESPONSE)


@dataclass
class Claim:
    """One line of one supplier document, as the rules read it."""
    line: str
    sku: str = ""
    upc: str = ""
    quantity: Decimal = Decimal("0")
    price: Optional[Decimal] = None
    status: str = ""
    reason: str = ""


@dataclass
class Received:
    """A supplier document being reconciled, and everything a rule may read."""
    partner: str
    kind: str
    code: str
    control: str
    interchange: str
    order: Dict[str, Any]
    lines: Dict[str, Dict[str, Any]]            # the order's lines, by number
    claims: List[Claim]                          # this document's
    earlier: List[Dict[str, Any]]                # every claim before it
    document: Any = None                         # what the reader returned
    found: List[BusinessFinding] = field(default_factory=list)

    def disagree(self, rule: str, line: str, expected, found, note: str) -> None:
        self.found.append(BusinessFinding(
            rule=rule, kind=self.kind, code=self.code, control=self.control,
            po_number=self.order["po_number"], line=line,
            expected=_text(expected), found=_text(found), note=note,
            interchange=self.interchange))


def record(conn, partner: Dict[str, Any], kind: str, code: str, control: str,
           interchange: str, document) -> List[BusinessFinding]:
    """File a supplier's document against the placed order it names.

    The caller has already matched it to an order the mock placed with this
    partner. Returns what disagrees, which is also stored.
    """
    order = documents.order_row(conn, document.po_number, partner["id"])
    received = Received(
        partner=partner["id"], kind=kind, code=code, control=control,
        interchange=interchange, order=order,
        lines={row["line"]: row for row in
               documents.order_lines(conn, order["po_number"], partner["id"])},
        claims=_claims_of(kind, document),
        earlier=claims_for(conn, partner["id"], order["po_number"]),
        document=document)
    # A consignment names an order line by its number when the 856 carries
    # one, and by its item when it does not.
    for claim in received.claims:
        if not claim.line:
            claim.line = _line_for_item(received.lines, claim)

    moment = db.now()
    reference = _reference(kind, document)
    for claim in received.claims:
        conn.execute(
            "INSERT INTO supplier_claim (partner, po_number, line, kind, code,"
            " control, interchange, document, status, sku, upc, quantity, price,"
            " reason, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (partner["id"], order["po_number"], claim.line, kind, code, control,
             interchange, reference, claim.status, claim.sku, claim.upc,
             quantity_text(claim.quantity),
             "" if claim.price is None else price_text(claim.price),
             claim.reason, moment))

    for rule in RULES.get(kind, ()):
        rule(received)
    for finding in received.found:
        conn.execute(
            "INSERT INTO disagreement (partner, po_number, line, rule, kind, code,"
            " control, interchange, expected, found, note, at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (partner["id"], finding.po_number, finding.line, finding.rule,
             finding.kind, finding.code, finding.control, finding.interchange,
             finding.expected, finding.found, finding.note, moment))
    derive(conn, partner["id"], order["po_number"])
    conn.commit()
    return received.found


def derive(conn, partner_id: str, po_number: str) -> None:
    """Recompute a placed order's confirmed, shipped and invoiced from its claims.

    What is confirmed is the latest answer for the line - an 855 corrected by
    another, or an 865 answering a change - and what shipped and was billed
    is every consignment and every bill added up - a bill sent twice under
    one invoice number counting once, since it is the same bill.
    """
    claims = claims_for(conn, partner_id, po_number)
    for row in documents.order_lines(conn, po_number, partner_id):
        mine = [claim for claim in claims if claim["line"] == row["line"]]
        answers = [claim for claim in mine if claim["kind"] in ANSWERS]
        latest = answers[-1] if answers else None
        conn.execute(
            "UPDATE order_line SET confirmed = ?, status = ?, reason = ?,"
            " shipped = ?, invoiced = ? WHERE partner = ? AND po_number = ?"
            " AND line = ?",
            (latest["quantity"] if latest else "0",
             latest["status"] if latest else "",
             latest["reason"] if latest else "",
             _sum(mine, schema.DESPATCH),
             quantity_text(_billed([c for c in mine
                                    if c["kind"] == schema.INVOICE]).get(
                 row["line"], Decimal("0"))),
             partner_id, po_number, row["line"]))


def claims_for(conn, partner_id: str, po_number: str) -> List[Dict[str, Any]]:
    return db.rows(conn, "SELECT * FROM supplier_claim WHERE partner = ?"
                         " AND po_number = ? ORDER BY id", (partner_id, po_number))


def disagreements(conn, partner_id: str = "", po_number: str = "",
                  limit: int = 0) -> List[Dict[str, Any]]:
    """Stored disagreements, oldest first, for whichever partner and order."""
    clauses, params = [], []
    if partner_id:
        clauses.append("partner = ?")
        params.append(partner_id)
    if po_number:
        clauses.append("po_number = ?")
        params.append(po_number)
    sql = "SELECT * FROM disagreement%s ORDER BY id" % (
        " WHERE " + " AND ".join(clauses) if clauses else "")
    if limit:
        sql += " LIMIT %d" % limit
    return db.rows(conn, sql, params)


def as_json(row: Dict[str, Any]) -> Dict[str, Any]:
    """A stored disagreement as the control plane shows it."""
    return {"rule": row["rule"], "partner": row["partner"],
            "order": row["po_number"], "line": row["line"], "kind": row["kind"],
            "code": row["code"], "control": row["control"],
            "interchange": row["interchange"], "expected": row["expected"],
            "found": row["found"], "note": row["note"], "at": row["at"]}


def finding_json(finding: BusinessFinding) -> Dict[str, Any]:
    return {"rule": finding.rule, "order": finding.po_number,
            "line": finding.line, "kind": finding.kind, "code": finding.code,
            "control": finding.control, "expected": finding.expected,
            "found": finding.found, "note": finding.note}


# ---------------------------------------------------------------------------
# Reading a document into claims
# ---------------------------------------------------------------------------

def _claims_of(kind: str, document) -> List[Claim]:
    if kind in ANSWERS:
        return [Claim(line=line.number, sku=line.sku, upc=line.upc,
                      quantity=line.confirmed, price=line.price or None,
                      status=line.status, reason=line.reason)
                for line in document.lines]
    if kind == schema.DESPATCH:
        return [Claim(line=item.line, sku=item.sku, upc=item.upc,
                      quantity=item.quantity) for item in document.items]
    if kind == schema.INVOICE:
        return [Claim(line=line.number, sku=line.sku, upc=line.upc,
                      quantity=line.quantity, price=line.price)
                for line in document.lines]
    return []


def _reference(kind: str, document) -> str:
    """The document's own number: the supplier's order, shipment or invoice."""
    if kind == schema.DESPATCH:
        return document.shipment_id
    if kind == schema.INVOICE:
        return document.invoice_number
    return getattr(document, "seller_order", "")


def _line_for_item(lines: Dict[str, Dict[str, Any]], claim: Claim) -> str:
    for number_, row in lines.items():
        if (claim.sku and claim.sku == row["sku"]) or (
                claim.upc and claim.upc == row["upc"]):
            return number_
    return ""


def _sum(claims: List[Dict[str, Any]], kind: str) -> str:
    return quantity_text(sum((number(claim["quantity"]) for claim in claims
                              if claim["kind"] == kind), Decimal("0")))


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return quantity_text(value)
    return str(value)


def _money(value: Decimal) -> str:
    return price_text(value)


# ---------------------------------------------------------------------------
# The rules for an 855, an ORDRSP, and an 865 answering the mock's change
# ---------------------------------------------------------------------------

def confirmed_a_line_not_ordered(received: Received) -> None:
    """A confirmation of a line the order does not have."""
    for claim in received.claims:
        if claim.line not in received.lines:
            received.disagree(
                "confirmed-unknown-line", claim.line, "", claim.quantity,
                "%s %s confirms line %s, %s of %s, which order %s does not have"
                % (received.code, received.control, claim.line or "(unnumbered)",
                   quantity_text(claim.quantity), claim.sku or claim.upc or "an item",
                   received.order["po_number"]))


def confirmed_more(received: Received) -> None:
    for claim, row in _known(received):
        ordered = number(row["quantity"])
        if claim.quantity > ordered:
            received.disagree(
                "confirmed-more", claim.line, ordered, claim.quantity,
                "line %s: ordered %s, %s confirms %s"
                % (claim.line, quantity_text(ordered), received.code,
                   quantity_text(claim.quantity)))


def confirmed_less(received: Received) -> None:
    """Less confirmed than ordered: a short line, or a refused one.

    One rule rather than two, deciding only what the sentence says: a short
    confirmation the supplier explained is still one the buyer has to act
    on, and one it claims is full acceptance (`IA`), or leaves unexplained,
    is worse and says so.
    """
    for claim, row in _known(received):
        ordered = number(row["quantity"])
        if claim.quantity >= ordered:
            continue
        if claim.status == ACCEPTED_AS_ORDERED:
            why = "and says %s, accepted as ordered" % claim.status
        elif not claim.reason:
            why = "%sand gives no reason" % ("%s, " % claim.status if claim.status
                                            else "")
        else:
            why = "%s: %s" % (claim.status or "no status", claim.reason)
        received.disagree(
            "confirmed-less", claim.line, ordered, claim.quantity,
            "line %s: ordered %s, %s confirms %s %s"
            % (claim.line, quantity_text(ordered), received.code,
               quantity_text(claim.quantity), why))


def restated_price(received: Received) -> None:
    for claim, row in _known(received):
        if claim.price is None or claim.quantity <= 0:
            continue
        ordered = number(row["ordered_price"], "0.00")
        if claim.price != ordered:
            received.disagree(
                "price-differs", claim.line, _money(ordered), _money(claim.price),
                "line %s: ordered at %s, %s says %s%s"
                % (claim.line, _money(ordered), received.code, _money(claim.price),
                   " (%s, price changed)" % claim.status
                   if claim.status == PRICE_CHANGED else ""))


def substituted_silently(received: Received) -> None:
    """A different item on the line, without `IS` to say it was substituted."""
    for claim, row in _known(received):
        if claim.status == SUBSTITUTED or claim.status == REJECTED:
            continue
        for label, ours, theirs in (("item", row["sku"], claim.sku),
                                    ("UPC", row["upc"], claim.upc)):
            if ours and theirs and ours != theirs:
                received.disagree(
                    "substituted", claim.line, ours, theirs,
                    "line %s: ordered %s %s, %s confirms %s %s without saying "
                    "it was substituted (%s)"
                    % (claim.line, label, ours, received.code, label, theirs,
                       SUBSTITUTED))
                break


def _known(received: Received):
    for claim in received.claims:
        row = received.lines.get(claim.line)
        if row is not None:
            yield claim, row


# ---------------------------------------------------------------------------
# The rules for an 810 or an INVOIC: the three-way match (#152)
# ---------------------------------------------------------------------------

def _earlier(received: Received, kind: str) -> List[Dict[str, Any]]:
    return [claim for claim in received.earlier if claim["kind"] == kind]


def _repeated(received: Received) -> bool:
    number_ = received.document.invoice_number
    return bool(number_) and any(claim["document"] == number_
                                 for claim in _earlier(received, schema.INVOICE))


def billed_before_shipped(received: Received) -> None:
    """An invoice with no 856 before it: billing for goods not yet advised."""
    if not _earlier(received, schema.DESPATCH):
        received.disagree(
            "billed-before-shipped", "", "an 856", "none",
            "%s %s bills order %s before any 856 for it"
            % (received.code, received.document.invoice_number or received.control,
               received.order["po_number"]))


def invoice_repeated(received: Received) -> None:
    """An invoice number already received for this order: the same bill twice."""
    if _repeated(received):
        received.disagree(
            "invoice-repeated", "", "", received.document.invoice_number,
            "invoice %s was already received for order %s"
            % (received.document.invoice_number, received.order["po_number"]))


def billed_more_than_shipped(received: Received) -> None:
    """Per line, everything billed so far against everything shipped.

    Only once something has shipped - an invoice with no 856 before it is
    `billed-before-shipped`, and saying it again per line is noise - and not
    for a repeated invoice, which is the same bill rather than more of it.
    """
    shipped_claims = _earlier(received, schema.DESPATCH)
    if not shipped_claims or _repeated(received):
        return
    billed_before = _billed(_earlier(received, schema.INVOICE))
    for claim, _row in _known(received):
        shipped = sum((number(c["quantity"]) for c in shipped_claims
                       if c["line"] == claim.line), Decimal("0"))
        billed = billed_before.get(claim.line, Decimal("0")) + claim.quantity
        if billed > shipped:
            received.disagree(
                "billed-more-than-shipped", claim.line, shipped, billed,
                "line %s: %s shipped, %s billed with %s %s"
                % (claim.line, quantity_text(shipped), quantity_text(billed),
                   received.code, received.document.invoice_number))


def price_not_agreed(received: Received) -> None:
    """A price that is neither what the mock ordered at nor what was confirmed."""
    answers = [claim for claim in received.earlier if claim["kind"] in ANSWERS]
    for claim, row in _known(received):
        if claim.price is None:
            continue
        ordered = number(row["ordered_price"], "0.00")
        confirmed = [number(c["price"], "0.00") for c in answers
                     if c["line"] == claim.line and c["price"]]
        agreed = {ordered} | ({confirmed[-1]} if confirmed else set())
        if claim.price not in agreed:
            received.disagree(
                "price-not-agreed", claim.line,
                " or ".join(sorted(_money(price) for price in agreed)),
                _money(claim.price),
                "line %s: billed at %s, ordered at %s%s"
                % (claim.line, _money(claim.price), _money(ordered),
                   ", confirmed at %s" % _money(confirmed[-1]) if confirmed else ""))


def total_not_lines(received: Received) -> None:
    """The invoice's own total against the sum of its own lines.

    Where the document states a subtotal - TDS02, or MOA+79 - that is what
    the lines must add up to; otherwise the total must be the lines plus the
    tax, as TXI or MOA+124 give it. Allowances and charges (SAC, ALC) are not
    read, so an invoice that carries one is compared without it.
    """
    invoice = received.document
    lines = invoice.line_total
    if invoice.subtotal is not None:
        stated, expected, what = invoice.subtotal, lines, "subtotal"
    else:
        stated, expected, what = invoice.total, lines + invoice.tax, "total"
    if stated != expected:
        received.disagree(
            "total-not-lines", "", _money(expected), _money(stated),
            "%s %s states a %s of %s, but its lines come to %s%s"
            % (received.code, invoice.invoice_number, what, _money(stated),
               _money(lines), " plus %s tax" % _money(invoice.tax)
               if what == "total" and invoice.tax else ""))


def billed_cancelled(received: Received) -> None:
    if received.order["status"] == "cancelled":
        received.disagree(
            "billed-cancelled", "", "cancelled", received.document.invoice_number,
            "%s %s bills order %s, which the mock cancelled"
            % (received.code, received.document.invoice_number,
               received.order["po_number"]))


def _billed(claims: List[Dict[str, Any]]) -> Dict[str, Decimal]:
    """What distinct invoices billed per line: a repeated one counts once."""
    seen, out = set(), {}
    for claim in claims:
        key = (claim["document"], claim["line"])
        if key in seen:
            continue
        seen.add(key)
        out[claim["line"]] = out.get(claim["line"], Decimal("0")) + number(
            claim["quantity"])
    return out


RULES: Dict[str, List[Callable[[Received], None]]] = {
    schema.RESPONSE: [confirmed_a_line_not_ordered, confirmed_more,
                      confirmed_less, restated_price, substituted_silently],
    schema.CHANGE_RESPONSE: [confirmed_a_line_not_ordered, confirmed_more,
                             confirmed_less, restated_price, substituted_silently],
    schema.DESPATCH: [],        # #151
    schema.INVOICE: [billed_before_shipped, invoice_repeated,
                     billed_more_than_shipped, price_not_agreed, total_not_lines,
                     billed_cancelled],
}
