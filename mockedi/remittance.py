"""What a remittance advice says that does not add up (#149, #156).

A remittance advice is read by the mock as the payee: it is acknowledged
if it can be read, and what is wrong with its *business* - rather than its
syntax - is a disagreement beside the 997, never in it (#116). The same class
carries a supplier's disagreements with an order the mock placed (#126); here
there is no order, so a finding names none.

Three rules, each a way a payer's integration gets the timing or the story
of a payment wrong (#149):

- the arithmetic: an advice that still lists an invoice whose payment came
  back, with a total that no longer adds up, is how a supplier ends up
  dunning for an invoice already paid;
- sent before the money settles: an 820 whose BPR16 effective date is still
  ahead of the mock's clock tells the payee to reconcile cash that has not
  arrived;
- a reversal of nothing: an 820 debiting (BPR03 D) a trace number no advice
  ever credited.

A reversal that *does* follow its advice is the correction, not a finding;
`listing` shows the advice it reversed as reversed. The timing and reversal
rules read the 820 only: which REMADV date is the value date, and how a
REMADV says it reverses another, vary too much between guides to guess.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from . import db, edifact, schema, x12
from .envelope import Message, parse_date
from .validate import BusinessFinding

TOTAL_NOT_PARTS = "remittance-total-not-parts"
BEFORE_SETTLEMENT = "remitted-before-settlement"
REVERSES_NOTHING = "reversal-of-nothing"

CREDIT, DEBIT = "C", "D"


@dataclass
class Advice:
    """What a remittance advice says, as far as the rules and the listing need."""
    trace: str = ""
    total: Optional[Decimal] = None
    credit_debit: str = CREDIT
    settles: Optional[datetime.date] = None
    invoices: List[Tuple[str, Optional[Decimal]]] = field(default_factory=list)


def read(message: Message, dialect: str) -> Advice:
    advice = Advice()
    if dialect == "X12":
        bpr = message.find("BPR")
        trn = message.find("TRN")
        if bpr is not None:
            advice.total = _amount(bpr.get(2))
            advice.credit_debit = bpr.get(3) or CREDIT
            advice.settles = parse_date(bpr.get(16))
        advice.trace = trn.get(2) if trn is not None else ""
        advice.invoices = [(item.get(2), _amount(item.get(4)))
                           for item in message.segments if item.tag == "RMR"]
        return advice
    bgm = message.find("BGM")
    advice.trace = bgm.comp(2, 1) if bgm is not None else ""
    within = ""
    for item in message.segments:
        if item.tag in ("DOC", "AJT", "UNS"):
            within = item.tag
            if item.tag == "DOC":
                advice.invoices.append((item.comp(2, 1), None))
        elif item.tag == "MOA" and item.comp(1, 1) == "12":
            if within == "DOC" and advice.invoices:
                advice.invoices[-1] = (advice.invoices[-1][0],
                                       _amount(item.comp(1, 2)))
            elif within == "UNS":
                advice.total = _amount(item.comp(1, 2))
    return advice


def findings(message: Message, dialect: str, kind: str, interchange: str,
             today: Optional[datetime.date] = None) -> List[BusinessFinding]:
    """Everything a remittance advice says that does not add up, or is early.

    `today` is the mock's clock, so `/_mock/advance` moves what "early" means.
    """
    out = _arithmetic(message, dialect, kind, interchange)
    if dialect == "X12" and today is not None:
        advice = read(message, dialect)
        if advice.settles is not None and advice.settles > today:
            out.append(BusinessFinding(
                rule=BEFORE_SETTLEMENT, kind=kind, code=message.code,
                control=message.control, po_number="",
                expected=advice.settles.isoformat(), found=today.isoformat(),
                note="the advice says the payment takes effect on %s (BPR16) "
                     "and arrived on %s: reconciled now, it is cash that has "
                     "not arrived" % (advice.settles.isoformat(),
                                      today.isoformat()),
                interchange=interchange))
    return out


def _arithmetic(message: Message, dialect: str, kind: str,
                interchange: str) -> List[BusinessFinding]:
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
           kind: str, interchange: str,
           today: Optional[datetime.date] = None) -> List[BusinessFinding]:
    """Find what disagrees, store it beside the supplier disagreements, return it.

    Called after the set itself is archived, so an earlier advice for the
    same trace is one of at least two rows.
    """
    found = findings(message, dialect, kind, interchange, today)
    if dialect == "X12":
        advice = read(message, dialect)
        if advice.credit_debit == DEBIT and advice.trace:
            credits, debits = _outstanding(conn, partner["id"], advice.trace)
            if debits > credits:
                # Nothing left to reverse: no credit for the trace at all, or
                # every one already taken back - a second reversal of the
                # same payment is the same mistake as a first of none.
                found.append(BusinessFinding(
                    rule=REVERSES_NOTHING, kind=kind, code=message.code,
                    control=message.control, po_number="",
                    expected="%d credit advice%s" % (credits,
                                                     "" if credits == 1 else "s"),
                    found="%d debits" % debits,
                    note="this 820 debits trace %s (BPR03 D), but %s"
                         % (advice.trace,
                            "no advice for that trace was received to reverse"
                            if not credits else
                            "every advice for that trace was already reversed"),
                    interchange=interchange))
    moment = db.now(conn)
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


def _outstanding(conn, partner_id: str, trace: str) -> Tuple[int, int]:
    """How many credit and debit advices this partner sent for the trace.

    The one in hand is archived already, so it is among them. Read from the
    advices themselves: a row's ST02 cannot tell them apart - it is 0001 in
    interchange after interchange - and only BPR03 says which way each went.
    """
    credits = debits = 0
    for row in _archived(conn, partner_id, trace):
        message = _message(conn, row)
        if message is None or row["dialect"] != "X12":
            continue
        if read(message, "X12").credit_debit == DEBIT:
            debits += 1
        else:
            credits += 1
    return credits, debits


def _archived(conn, partner_id: str = "", trace: str = "") -> List[Dict[str, Any]]:
    clauses = ["direction = 'in'", "kind = ?", "accepted = 1"]
    params: List[Any] = [schema.REMITTANCE]
    if partner_id:
        clauses.append("partner = ?")
        params.append(partner_id)
    if trace:
        clauses.append("reference = ?")
        params.append(trace)
    return db.rows(conn, "SELECT * FROM transaction_set WHERE %s ORDER BY id"
                   % " AND ".join(clauses), params)


def listing(conn, partner_id: str = "") -> List[Dict[str, Any]]:
    """Every accepted remittance advice, oldest first, with what became of it.

    Read back from the archive rather than kept anywhere of its own. A credit
    advice followed by a debit for the same partner and trace is `reversed`,
    naming the advice that reversed it - the correction a payer owes once a
    payment comes back (a pacs.004 from the bank). `settledOnArrival` is
    whether the advice was judged remitted before settlement when it came -
    read from that stored finding, so it answers by the mock's clock, as the
    finding did, rather than by the real one a later advance leaves behind.
    """
    out: List[Dict[str, Any]] = []
    for row in _archived(conn, partner_id):
        message = _message(conn, row)
        if message is None:
            continue
        advice = read(message, row["dialect"])
        out.append({
            "partner": row["partner"], "dialect": row["dialect"],
            "code": row["code"], "control": row["control"], "id": row["id"],
            "trace": advice.trace,
            "total": None if advice.total is None else str(advice.total),
            "creditDebit": advice.credit_debit,
            "settles": advice.settles.isoformat() if advice.settles else "",
            "settledOnArrival": (None if advice.settles is None
                                 else not _was_early(conn, row)),
            "invoices": [{"invoice": invoice,
                          "paid": None if paid is None else str(paid)}
                         for invoice, paid in advice.invoices],
            "at": row["at"], "status": "reversal" if advice.credit_debit == DEBIT
                                        else "advised", "reversedBy": None,
        })
    for index, item in enumerate(out):
        if item["creditDebit"] != DEBIT or not item["trace"]:
            continue
        for earlier in out[:index]:
            if (earlier["partner"], earlier["trace"]) == (item["partner"],
                                                          item["trace"]) \
                    and earlier["status"] == "advised":
                earlier["status"] = "reversed"
                # The archive's id: ST02 repeats between interchanges.
                earlier["reversedBy"] = item["id"]
    return out


def _was_early(conn, row: Dict[str, Any]) -> bool:
    """Whether this archived set was found remitted before settlement."""
    return db.one(
        conn, "SELECT 1 AS found FROM disagreement d JOIN interchange i"
              " ON i.control = d.interchange WHERE d.rule = ? AND d.partner = ?"
              " AND d.code = ? AND d.control = ? AND i.id = ?",
        (BEFORE_SETTLEMENT, row["partner"], row["code"], row["control"],
         row["interchange_id"])) is not None


def _message(conn, row: Dict[str, Any]) -> Optional[Message]:
    """The archived transaction set itself, read back out of its interchange."""
    stored = db.one(conn, "SELECT payload FROM interchange WHERE id = ?",
                    (row["interchange_id"],))
    if stored is None:
        return None
    try:
        parsed = (x12.parse if row["dialect"] == "X12" else edifact.parse)(
            stored["payload"])
    except Exception:            # an archive row the parser no longer reads
        return None
    for _group, message in parsed.messages():
        if message.code == row["code"] and message.control == row["control"]:
            return message
    return None


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

    An empty amount - an RMR with no RMR04 - counts as nothing paid on it.
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
