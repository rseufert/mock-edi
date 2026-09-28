"""What a remittance advice says that does not add up (#149, #156).

A remittance advice is read by the mock as the payee: it is acknowledged
if it can be read, and what is wrong with its *business* - rather than its
syntax - is a disagreement beside the 997, never in it (#116). The same class
carries a supplier's disagreements with an order the mock placed (#126); here
there is no order, so a finding names none.

The one rule so far is the arithmetic. An advice that still lists an invoice
whose payment came back, with a total that no longer adds up, is how a
supplier ends up dunning for an invoice already paid.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from . import db
from .envelope import Message
from .validate import BusinessFinding

TOTAL_NOT_PARTS = "remittance-total-not-parts"


def findings(message: Message, dialect: str, kind: str,
             interchange: str) -> List[BusinessFinding]:
    """Everything a remittance advice says that does not add up."""
    found = _total_not_parts(message, dialect)
    if found is None:
        return []
    total, parts, counted = found
    if dialect == "X12":
        note = ("BPR02 says %s was paid, but the %s it lists come to %s"
                % (total, counted, parts))
    else:
        note = ("the MOA+12 after UNS says %s was remitted, but the %s it "
                "lists come to %s" % (total, counted, parts))
    return [BusinessFinding(
        rule=TOTAL_NOT_PARTS, kind=kind, code=message.code,
        control=message.control, po_number="", expected=str(parts),
        found=str(total), note=note, interchange=interchange)]


def record(conn, partner: Dict[str, Any], message: Message, dialect: str,
           kind: str, interchange: str) -> List[BusinessFinding]:
    """Find what disagrees, store it beside the supplier disagreements, return it."""
    found = findings(message, dialect, kind, interchange)
    moment = db.now()
    for finding in found:
        conn.execute(
            "INSERT INTO disagreement (partner, po_number, line, rule, kind, code,"
            " control, interchange, expected, found, note, at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (partner["id"], "", "", finding.rule, finding.kind, finding.code,
             finding.control, finding.interchange, finding.expected,
             finding.found, finding.note, moment))
    if found:
        conn.commit()
    return found


def _total_not_parts(message: Message,
                     dialect: str) -> Optional[Tuple[Decimal, Decimal, str]]:
    """(total, sum of the parts, what was counted) when they differ, else None.

    X12: BPR02 against the RMR04 amounts plus every ADX01 directly in an ENT
    loop. An ADX inside an RMR loop is already in that RMR04; one at the ENT
    level is a deduction or credit not tied to one invoice, which BPR02 pays
    and no RMR04 does - leaving it out would call a correct 820 wrong.

    EDIFACT: the MOA+12 after UNS against each DOC group's own MOA+12; an AJT
    group's amounts are already in its DOC's. With no MOA+12 total nothing is
    claimed, since a guide may name its total otherwise.

    A number that does not parse is the syntax check's to report; nothing is
    claimed about arithmetic that cannot be done.
    """
    if dialect == "X12":
        bpr = message.find("BPR")
        total = _amount(bpr.get(2)) if bpr is not None else None
        if total is None:
            return None
        invoices: List[Decimal] = []
        adjustments: List[Decimal] = []
        within = ""
        for item in message.segments:
            if item.tag in ("ENT", "RMR"):
                within = item.tag
            if item.tag == "RMR":
                invoices.append(_amount(item.get(4)))
            elif item.tag == "ADX" and within == "ENT":
                adjustments.append(_amount(item.get(1)))
        if any(value is None for value in invoices + adjustments):
            return None
        parts = sum(invoices + adjustments, Decimal("0"))
        counted = _counted(len(invoices), "RMR04 amount", len(adjustments),
                           "entity-level adjustment")
    else:
        per_document: List[Decimal] = []
        total = None
        within = ""
        for item in message.segments:
            if item.tag in ("DOC", "AJT", "UNS"):
                within = item.tag
            elif item.tag == "MOA" and item.comp(1, 1) == "12":
                amount = _amount(item.comp(1, 2))
                if amount is None:
                    return None
                if within == "DOC":
                    per_document.append(amount)
                elif within == "UNS":
                    total = amount
        if total is None:
            return None
        parts = sum(per_document, Decimal("0"))
        counted = _counted(len(per_document), "document amount", 0, "")
    if total == parts:
        return None
    return total, parts, counted


def _counted(first: int, first_name: str, second: int, second_name: str) -> str:
    said = "%d %s%s" % (first, first_name, "" if first == 1 else "s")
    if second:
        said += " and %d %s%s" % (second, second_name, "" if second == 1 else "s")
    return said


def _amount(value: str) -> Optional[Decimal]:
    try:
        return Decimal((value or "0").strip() or "0")
    except (InvalidOperation, ValueError):
        return None
