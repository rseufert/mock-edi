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
are all here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from . import db, documents, schema
from .money import unit_price
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
    # Set when the item named matches more than one line of the order, so the
    # claim stays unplaced and `shipped_an_item_two_lines_ordered` reports it
    # rather than the mock picking one (#151).
    ambiguous: bool = False


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
            _place(received.lines, claim)

    moment = db.now(conn)
    reference = _reference(kind, document)
    for claim in received.claims:
        conn.execute(
            "INSERT INTO supplier_claim (partner, po_number, line, kind, code,"
            " control, interchange, document, status, sku, upc, quantity, price,"
            " reason, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (partner["id"], order["po_number"], claim.line, kind, code, control,
             interchange, reference, claim.status, claim.sku, claim.upc,
             quantity_text(claim.quantity),
             "" if claim.price is None else unit_price(claim.price),
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

    With one floor: **a later answer cannot un-confirm what an earlier one
    confirmed and the supplier has already shipped against** (#168). A
    supplier that refuses a *second* order under a number it has already
    shipped against sends an 855 that is, on the wire, indistinguishable
    from one rejecting the order outright - every line `IR` with a reason -
    so there is nothing to tell them apart by. Taken as the latest answer it
    said nothing was confirmed while a hundred were shipped and billed,
    which is a wrong picture of a transaction that went right.

    Note what the floor is *not*. It is not "confirmed is at least what
    shipped". A supplier that confirms 100 and ships 200 has confirmed 100,
    and saying otherwise would quietly answer the question
    `shipped-more-than-confirmed` exists to ask. The floor only stops a
    later answer taking back an earlier one, and only as far as what has
    actually shipped.

    It hides nothing either. The claims are all kept, and what the document
    said is still reported as a disagreement naming both numbers - it is
    only the running total that stops contradicting itself.
    """
    claims = claims_for(conn, partner_id, po_number)
    for row in documents.order_lines(conn, po_number, partner_id):
        mine = [claim for claim in claims if claim["line"] == row["line"]]
        answers = [claim for claim in mine if claim["kind"] in ANSWERS]
        latest = answers[-1] if answers else None
        shipped = _sum(mine, schema.DESPATCH)
        confirmed = number(latest["quantity"]) if latest else Decimal("0")
        speaking = latest
        if latest is not None and len(answers) > 1:
            # Only what had shipped *before this answer arrived*: a
            # consignment that comes after a correction was shipped against
            # the correction, and the correction stands.
            sent = sum((number(claim["quantity"]) for claim in mine
                        if claim["kind"] == schema.DESPATCH
                        and claim["id"] < latest["id"]), Decimal("0"))
            for answer in answers[:-1]:
                held = min(sent, number(answer["quantity"]))
                if held > confirmed:
                    # That earlier answer still stands for this much, so the
                    # line's status and reason are its word, not the one that
                    # tried to take it back.
                    confirmed, speaking = held, answer
        conn.execute(
            "UPDATE order_line SET confirmed = ?, status = ?, reason = ?,"
            " shipped = ?, invoiced = ? WHERE partner = ? AND po_number = ?"
            " AND line = ?",
            (quantity_text(confirmed),
             speaking["status"] if speaking else "",
             speaking["reason"] if speaking else "",
             shipped,
             quantity_text(_billed([c for c in mine
                                    if c["kind"] == schema.INVOICE]).get(
                 row["line"], Decimal("0"))),
             partner_id, po_number, row["line"]))


def claims_for(conn, partner_id: str, po_number: str) -> List[Dict[str, Any]]:
    return db.rows(conn, "SELECT * FROM supplier_claim WHERE partner = ?"
                         " AND po_number = ? ORDER BY id", (partner_id, po_number))


def sellers_order(conn, partner_id: str, po_number: str) -> str:
    """The number the supplier filed a placed order under, once it has said.

    From its latest 855 or 865: `BAK08`, `BCA09` or `REF*VN`. Empty until one
    arrives that gives it.
    """
    row = db.one(conn, "SELECT document FROM supplier_claim WHERE partner = ?"
                       " AND po_number = ? AND kind IN (?, ?) AND document != ''"
                       " ORDER BY id DESC LIMIT 1",
                 (partner_id, po_number, schema.RESPONSE, schema.CHANGE_RESPONSE))
    return row["document"] if row else ""


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


def _lines_for_item(lines: Dict[str, Dict[str, Any]], claim: Claim) -> List[str]:
    """Every order line the item named could be, by SKU first and then UPC.

    SKU before UPC rather than either-or: an exact SKU match is the stronger
    statement, and taking whichever came first in the order let a line whose
    UPC happened to collide win over the line that actually ordered the item.
    """
    for field_, ours in (("sku", claim.sku), ("upc", claim.upc)):
        if not ours:
            continue
        found = [number_ for number_, row in lines.items()
                 if row[field_] and row[field_] == ours]
        if found:
            return found
    return []


def _place(lines: Dict[str, Dict[str, Any]], claim: Claim) -> None:
    """Put an unnumbered claim on its line, or leave it unplaced and say why.

    The same item on two lines - the same SKU twice for two dates or two
    ship-tos - is ordinary, and nothing in the document says which is meant.
    Guessing produced a quantity against one line and silence against the
    other; the claim stays unplaced instead, and a rule reports it.
    """
    found = _lines_for_item(lines, claim)
    if len(found) == 1:
        claim.line = found[0]
    else:
        claim.ambiguous = len(found) > 1


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
                "price-differs", claim.line, unit_price(ordered),
                unit_price(claim.price),
                "line %s: ordered at %s, %s says %s%s"
                % (claim.line, unit_price(ordered), received.code,
                   unit_price(claim.price),
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
                " or ".join(sorted(unit_price(price) for price in agreed)),
                unit_price(claim.price),
                "line %s: billed at %s, ordered at %s%s"
                % (claim.line, unit_price(claim.price), unit_price(ordered),
                   ", confirmed at %s" % unit_price(confirmed[-1])
                   if confirmed else ""))


def total_not_lines(received: Received) -> None:
    """The invoice's own totals against the sum of its own lines.

    Two comparisons, and at most one finding: EDIFACT's MOA+79 is the total of
    the line items and must be what they add up to; and the total - TDS01, or
    MOA+139 - must be the lines plus the invoice's allowances and charges
    plus its tax (#158). TDS02 is not compared: it is the amount subject to
    terms discount, which freight, say, is commonly not.

    The total is not judged when it cannot be: an allowance given only as a
    percentage, or an INVOIC that states no MOA+139.
    """
    invoice = received.document
    lines = invoice.line_total
    if invoice.line_items_total is not None and invoice.line_items_total != lines:
        received.disagree(
            "total-not-lines", "", _money(lines), _money(invoice.line_items_total),
            "%s %s states its line items come to %s, but its lines come to %s"
            % (received.code, invoice.invoice_number,
               _money(invoice.line_items_total), _money(lines)))
        return
    if not (invoice.total_stated and invoice.charges_known):
        return
    expected = lines + invoice.charges + invoice.tax
    if invoice.total != expected:
        parts = ["lines %s" % _money(lines)]
        if invoice.charges:
            parts.append("%s %s" % ("charges" if invoice.charges > 0 else "allowances",
                                    _money(abs(invoice.charges))))
        if invoice.tax:
            parts.append("tax %s" % _money(invoice.tax))
        received.disagree(
            "total-not-lines", "", _money(expected), _money(invoice.total),
            "%s %s states a total of %s, but %s come to %s"
            % (received.code, invoice.invoice_number, _money(invoice.total),
               ", ".join(parts), _money(expected)))


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


# ---------------------------------------------------------------------------
# The rules for an 856 or a DESADV: what was sent against what was promised (#151)
# ---------------------------------------------------------------------------

def _shipped_before(received: Received, line: str) -> Decimal:
    """What earlier consignments already put on this line."""
    return sum((number(claim["quantity"])
                for claim in _earlier(received, schema.DESPATCH)
                if claim["line"] == line), Decimal("0"))


def _confirmed_for(received: Received, line: str) -> Optional[Dict[str, Any]]:
    """The latest answer for a line, which is what the supplier promised."""
    answers = [claim for claim in received.earlier
               if claim["kind"] in ANSWERS and claim["line"] == line]
    return answers[-1] if answers else None


def _consignment(received: Received) -> str:
    return received.document.shipment_id or received.control


def shipped_before_confirmed(received: Received) -> None:
    """A despatch with no 855 before it: goods sent against nothing promised."""
    if not [claim for claim in received.earlier if claim["kind"] in ANSWERS]:
        received.disagree(
            "shipped-before-confirmed", "", "an 855", "none",
            "%s %s ships against order %s before any 855 for it"
            % (received.code, _consignment(received),
               received.order["po_number"]))


def shipped_a_line_not_ordered(received: Received) -> None:
    """A consignment against a line the order does not have."""
    for claim in received.claims:
        if claim.ambiguous or claim.line in received.lines:
            continue
        received.disagree(
            "shipped-unknown-line", claim.line, "", claim.quantity,
            "%s %s ships %s of %s against line %s, which order %s does not have"
            % (received.code, _consignment(received),
               quantity_text(claim.quantity),
               claim.sku or claim.upc or "an item",
               claim.line or "(unnumbered)", received.order["po_number"]))


def shipped_an_item_two_lines_ordered(received: Received) -> None:
    """A consignment naming an item, with no line number, that two lines ordered.

    The mock will not choose between them: nothing in the document says which
    is meant, and putting the quantity on one silently leaves the other short
    for a reason nobody can see.
    """
    for claim in received.claims:
        if not claim.ambiguous:
            continue
        which = ", ".join(_lines_for_item(received.lines, claim))
        received.disagree(
            "shipped-ambiguous-item", "", which, claim.sku or claim.upc,
            "%s %s ships %s of %s with no line number, and order %s has it on "
            "lines %s, so it is counted against none of them"
            % (received.code, _consignment(received),
               quantity_text(claim.quantity), claim.sku or claim.upc or "an item",
               received.order["po_number"], which))


def shipped_more_than_confirmed(received: Received) -> None:
    """Everything shipped for a line so far, against what the 855 promised.

    Only once something has been confirmed - a despatch before any 855 is
    `shipped-before-confirmed`, and saying it again per line is noise, which
    is how #152's `billed_more_than_shipped` treats the same situation. A
    line confirmed at nothing is the rejected case, and says so.
    """
    if not [claim for claim in received.earlier if claim["kind"] in ANSWERS]:
        return
    for claim, _row in _known(received):
        answer = _confirmed_for(received, claim.line)
        if answer is None:
            continue
        confirmed = number(answer["quantity"])
        shipped = _shipped_before(received, claim.line) + claim.quantity
        if shipped <= confirmed:
            continue
        received.disagree(
            "shipped-more-than-confirmed", claim.line, confirmed, shipped,
            "line %s: %s, %s shipped with %s %s"
            % (claim.line,
               "confirmed at nothing, refused (%s)" % answer["status"]
               if confirmed <= 0 and answer["status"] else
               "confirmed %s" % quantity_text(confirmed),
               quantity_text(shipped), received.code, _consignment(received)))


def shipped_more_than_ordered(received: Received) -> None:
    """Everything shipped for a line so far, against what the mock asked for.

    Separate from the rule above, and both can fire on one line: *you sent
    more than you promised* and *you sent more than I asked for* are different
    sentences to a buyer, and when no 855 ever arrived this is the only one of
    the two that can be said at all.
    """
    for claim, row in _known(received):
        ordered = number(row["quantity"])
        shipped = _shipped_before(received, claim.line) + claim.quantity
        if shipped > ordered:
            received.disagree(
                "shipped-more-than-ordered", claim.line, ordered, shipped,
                "line %s: ordered %s, %s shipped with %s %s"
                % (claim.line, quantity_text(ordered), quantity_text(shipped),
                   received.code, _consignment(received)))


RULES: Dict[str, List[Callable[[Received], None]]] = {
    schema.RESPONSE: [confirmed_a_line_not_ordered, confirmed_more,
                      confirmed_less, restated_price, substituted_silently],
    schema.CHANGE_RESPONSE: [confirmed_a_line_not_ordered, confirmed_more,
                             confirmed_less, restated_price, substituted_silently],
    schema.DESPATCH: [shipped_before_confirmed,
                      shipped_a_line_not_ordered,
                      shipped_an_item_two_lines_ordered,
                      shipped_more_than_confirmed,
                      shipped_more_than_ordered],
    schema.INVOICE: [billed_before_shipped, invoice_repeated,
                     billed_more_than_shipped, price_not_agreed, total_not_lines,
                     billed_cancelled],
}
