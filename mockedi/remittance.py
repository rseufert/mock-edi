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
from .envelope import Message, parse_date, parse_edifact_date
from .validate import BusinessFinding

TOTAL_NOT_PARTS = "remittance-total-not-parts"
# An advice whose currency is not the currency of an invoice it pays. The
# amounts can agree to the penny and still be two different sums of money,
# which is the join that cannot be made from an amount alone (#281).
CURRENCY_NOT_THE_INVOICE = "remittance-currency-not-the-invoice"
# A REMADV whose header CUX and whose summary MOA name different currencies.
# D.96A permits both to state one and gives no rule for which wins - CUX
# recurs in SG3, SG5 and SG9, and C516 carries 6345 itself - so the mock
# reports the disagreement rather than picking a side silently (#281).
CURRENCY_DISAGREES = "remittance-currency-disagrees"
# A DOC group stating more than one amount remitted. D.96A allows five MOA
# in the group and `validate` accepts them, so the document is correct and
# the mock must say which it took rather than keep the last one quietly
# (#325). Its own rule and not `remittance-currency-disagrees`, because
# that one is about currency and this is about money.
AMOUNTS_REPEATED = "remittance-amounts-repeated"
# 6347's code for the currency an advice's amounts are in: "Reference
# currency - the currency applicable to amounts stated. It may have to be
# converted." Which is what a total is in, where the other codes are about
# conversion, accounts and information.
REFERENCE_CURRENCY = "2"
BEFORE_SETTLEMENT = "remitted-before-settlement"
REVERSES_NOTHING = "reversal-of-nothing"
# A REMADV that replaces an advice the mock never received from that
# partner. The counterpart of `reversal-of-nothing`, which is X12's shape
# for the same mistake: a correction of something that was never said.
REPLACES_NOTHING = "replacement-of-nothing"
# 2005's code for a REMADV's settlement date, the counterpart of an 820's
# BPR16: "Date on which an amount due is made available to the creditor, in
# accordance with the terms of payment." EANCOM's REMADV admits 137, 138,
# 203, 227 and 263 at the head, and not 209, and says each advice relates
# to one settlement date (#187).
SETTLEMENT_EDIFACT = "138"
# BGM 1225's code for a replacement, and 1153's for the advice it names.
REPLACEMENT = "5"
REPLACED_ADVICE = "RA"

CREDIT, DEBIT = "C", "D"


@dataclass
class Paid:
    """One invoice an advice pays: its number, the amount, and its currency.

    The currency is the document's own and None where the document states
    none - which is the ordinary case and means "the advice's". X12 has no
    per-invoice currency to read: `CUR` is one per message, outside the loop
    that holds the invoices (#298).

    A document can say it two ways, as the advice itself can one level up,
    and the two levels resolve it the same way (#322): the `MOA`'s own
    currency, `C516`'s `6345`, is the document's, and the `CUX` in its group
    (D.96A's SG5) is the fallback. `group_currency` keeps what that `CUX`
    said, so that a group naming two can be reported rather than silently
    resolved - the same reason `Advice.header_currency` is kept apart from
    `Advice.currency`.
    """
    invoice: str = ""
    paid: Optional[Decimal] = None
    currency: Optional[str] = None
    # What the CUX in this document's group said. The counterpart of
    # `Advice.header_currency`, and the fallback for `currency` above.
    group_currency: Optional[str] = None
    # Every currency the group's CUX segments name, and every amount its
    # MOA+12 segments state, both in document order. The group admits five
    # of each and `validate` accepts them, so the mock reports the extras
    # rather than dropping them - which is what the header does with its
    # own repeated CUX, and did not do here (#325).
    group_currencies: List[str] = field(default_factory=list)
    amounts: List[Decimal] = field(default_factory=list)


@dataclass
class Advice:
    """What a remittance advice says, as far as the rules and the listing need."""
    trace: str = ""
    total: Optional[Decimal] = None
    credit_debit: str = CREDIT
    settles: Optional[datetime.date] = None
    invoices: List["Paid"] = field(default_factory=list)
    # The currency the total is in: CUR02 for an 820, the summary MOA's 6345
    # for a REMADV, falling back to the header CUX. None when the advice
    # states none, which is not the same as "USD" and is not guessed: the
    # orders and invoices this mock writes state theirs, and a default here
    # would put a guess beside facts (#281).
    currency: Optional[str] = None
    # What the header CUX said, kept apart so that a REMADV naming two can be
    # reported. Always None for X12, which has one CUR at heading level and
    # none inside the loop that holds the invoices.
    header_currency: Optional[str] = None
    # Every currency the header CUX segments name, in document order. The
    # group repeats up to nine times, so a REMADV can name several and the
    # mock reports that rather than keeping one quietly.
    header_currencies: List[str] = field(default_factory=list)
    # BGM 1225, what the advice is for. EANCOM's REMADV admits 9 Original,
    # 31 Copy and 5 Replace, and 5 is the only one the mock acts on (#187).
    # Empty for X12, which says this positionally and differently.
    purpose: str = ""
    # The advice this one replaces, from the header RFF+RA where 1225 is 5.
    # The number is another advice's BGM, so it is matched against `trace`.
    replaces: str = ""


def _edifact_day(item):
    """The date in a DTM, by the format its third component states (#209)."""
    return parse_edifact_date(item.comp(1, 2), item.comp(1, 3))


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
        cur = message.find("CUR")
        advice.currency = (cur.get(2) or None) if cur is not None else None
        advice.invoices = [Paid(item.get(2), _amount(item.get(4)))
                           for item in message.segments if item.tag == "RMR"]
        return advice
    bgm = message.find("BGM")
    advice.trace = bgm.comp(2, 1) if bgm is not None else ""
    advice.purpose = bgm.get(3) if bgm is not None else ""
    within = ""
    header: List[Tuple[str, Optional[str]]] = []
    for item in message.segments:
        if item.tag in ("DOC", "AJT", "UNS"):
            within = item.tag
            if item.tag == "DOC":
                advice.invoices.append(Paid(item.comp(2, 1)))
        elif item.tag == "CUX":
            # C504's qualifier and its currency. D.96A's SG3 admits five at
            # the head, each qualified, so there may be several: the advice's
            # amounts are in the *reference* currency, 6347 code 2, whose own
            # definition is "the currency applicable to amounts stated".
            # Collected rather than overwritten, because keeping the last one
            # silently is the same silence #281 removed a level up.
            #
            # Inside a DOC group it is SG5's, and it is that document's own -
            # which is how an advice paying invoices in two currencies says
            # so. Declared since #298; before that the segment was reported
            # as unexpected and this read nothing.
            if within == "DOC" and advice.invoices:
                if item.comp(1, 2):
                    advice.invoices[-1].group_currencies.append(
                        item.comp(1, 2))
            elif not within:
                # The header's own: before any DOC, AJT or UNS. Not `within
                # not in ("DOC", "AJT")`, which counted a CUX *after* UNS as
                # the header's and so could report a disagreement about a
                # segment that is not a header CUX at all. The dictionary
                # declares none in the summary section, but the validator
                # checks which segments a set may hold and not where they
                # sit, so the reader cannot lean on that.
                header.append((item.comp(1, 1), item.comp(1, 2) or None))
        elif item.tag == "DTM" and not within:
            # The header's, and only the header's. A DTM inside a DOC group
            # dates that document and one after UNS is nobody's; counting
            # either as the advice's is the fault #315 had with CUX, so the
            # guard is here from the start rather than added afterwards.
            if item.comp(1, 1) == SETTLEMENT_EDIFACT:
                advice.settles = advice.settles or _edifact_day(item)
        elif item.tag == "RFF" and not within:
            if item.comp(1, 1) == REPLACED_ADVICE:
                advice.replaces = advice.replaces or item.comp(1, 2)
        elif item.tag == "MOA" and item.comp(1, 1) == "12":
            if within == "DOC" and advice.invoices:
                # An empty 5004 states no amount; it is not a statement of
                # zero. Taking it as the first would answer `0` for a group
                # whose next MOA says 40.00, which is worse than the last-
                # wins this replaced. A written `0` is stated and counts.
                amount = (_amount(item.comp(1, 2))
                          if item.comp(1, 2).strip() else None)
                if amount is not None:
                    advice.invoices[-1].amounts.append(amount)
                if advice.invoices[-1].paid is None and amount is not None:
                    # The first, not the last: a second MOA+12 in one group
                    # used to replace the first and the money in it was gone
                    # (#325). C516's third component comes from the same
                    # segment, so a document's amount and the currency it is
                    # in can never come from two different places (#322).
                    advice.invoices[-1].paid = amount
                    advice.invoices[-1].currency = item.comp(1, 3) or None
            elif within == "UNS":
                advice.total = _amount(item.comp(1, 2))
                # C516's third component, which the summary MOA may state
                # even where the header CUX does not.
                advice.currency = item.comp(1, 3) or None
    stated = [currency for _qualifier, currency in header if currency]
    reference = [currency for qualifier, currency in header
                 if currency and qualifier == REFERENCE_CURRENCY]
    advice.header_currency = (reference or stated or [None])[0]
    advice.header_currencies = stated
    advice.currency = advice.currency or advice.header_currency
    for paid in advice.invoices:
        # After the loop, not inside it: a group's CUX may sit either side
        # of its MOA and neither order should decide which currency wins.
        paid.group_currency = (paid.group_currencies or [None])[0]
        paid.currency = paid.currency or paid.group_currency
    return advice


def findings(message: Message, dialect: str, kind: str, interchange: str,
             today: Optional[datetime.date] = None) -> List[BusinessFinding]:
    """Everything a remittance advice says that does not add up, or is early.

    `today` is the mock's clock, so `/_mock/advance` moves what "early" means.
    """
    out = _arithmetic(message, dialect, kind, interchange)
    if today is not None:
        advice = read(message, dialect)
        if advice.settles is not None and advice.settles > today:
            # Both dialects now: BPR16 for an 820, the header DTM+138 for a
            # REMADV (#187). The note names the segment of the document in
            # hand, because telling an EDIFACT caller about BPR16 sends them
            # looking for a segment their message does not have.
            where = "BPR16" if dialect == "X12" else "DTM+138"
            out.append(BusinessFinding(
                rule=BEFORE_SETTLEMENT, kind=kind, code=message.code,
                control=message.control, po_number="",
                expected=advice.settles.isoformat(), found=today.isoformat(),
                note="the advice says the payment takes effect on %s (%s) "
                     "and arrived on %s: reconciled now, it is cash that has "
                     "not arrived" % (advice.settles.isoformat(), where,
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
    advice = read(message, dialect)
    if dialect == "EDIFACT" and advice.purpose == REPLACEMENT \
            and advice.replaces:
        if not _advices_numbered(conn, partner["id"], advice.replaces,
                                 advice.trace):
            # EANCOM's shape for the mistake `reversal-of-nothing` catches
            # in an 820: a correction of something never said. There is no
            # REMADV debit and no negative-amount convention to look for -
            # no source has one - so this is the whole of it (#187).
            found.append(BusinessFinding(
                rule=REPLACES_NOTHING, kind=kind, code=message.code,
                control=message.control, po_number="",
                expected=advice.replaces, found="",
                note="this REMADV replaces advice %s (BGM 1225 code 5, "
                     "RFF+RA), but no advice numbered %s was received from "
                     "%s" % (advice.replaces, advice.replaces, partner["id"]),
                interchange=interchange))
    found.extend(_currency_findings(conn, partner, advice, message, kind,
                                    interchange))
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


def _advices_numbered(conn, partner_id: str, number: str, own: str) -> int:
    """How many advices this partner sent carrying `number` as their own.

    `reference` holds an inbound REMADV's BGM number, so the filter is in
    SQL - but it is confirmed against the advice itself, as `_outstanding`
    does and for the same reason: the column is a convenience and the
    message is the fact.

    The advice in hand is archived before this runs, so one that names its
    own number is naming itself and has replaced nothing.
    """
    count = 0
    for row in _archived(conn, partner_id, number):
        message = _message(conn, row)
        if message is None or row["dialect"] != "EDIFACT":
            continue
        if read(message, "EDIFACT").trace == number:
            count += 1
    return count - (1 if own == number else 0)


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


def _currency_findings(conn, partner: Dict[str, Any], advice: Advice,
                       message: Message, kind: str,
                       interchange: str) -> List[BusinessFinding]:
    """What the advice says about currency that does not hold up.

    Two separate things, and they are separate on purpose. One is the advice
    disagreeing with the invoices it pays, which both dialects can do. The
    other is a REMADV disagreeing with *itself* - a header CUX and a summary
    MOA naming different currencies - which D.96A permits and gives no rule
    for, so the mock reports it rather than choosing a side.

    An advice that states no currency is not a finding. It is a thing a real
    advice does, the listing says so with a null, and inventing one to
    compare against would be the guess #281 asks us not to make.
    """
    out: List[BusinessFinding] = []
    others = [currency for currency in advice.header_currencies
              if currency != advice.header_currency]
    if others:
        # Several header CUX segments naming different currencies. The
        # reference one (6347 code 2) is taken as the advice's, and the rest
        # are said out loud rather than dropped: D.96A permits up to nine and
        # does not say they must agree.
        out.append(BusinessFinding(
            rule=CURRENCY_DISAGREES, kind=kind, code=message.code,
            control=message.control, po_number="",
            expected=advice.header_currency or "",
            found=", ".join(sorted(set(others))),
            note="the header names %s; %s taken as the advice's, being the "
                 "reference currency, and D.96A does not say they must agree"
                 % (", ".join(sorted(set(advice.header_currencies))),
                    advice.header_currency),
            interchange=interchange))
    if (advice.header_currency and advice.currency
            and advice.header_currency != advice.currency):
        out.append(BusinessFinding(
            rule=CURRENCY_DISAGREES, kind=kind, code=message.code,
            control=message.control, po_number="",
            expected=advice.header_currency, found=advice.currency,
            note="the header CUX says %s and the summary MOA says %s; D.96A "
                 "lets both state one and does not say which wins"
                 % (advice.header_currency, advice.currency),
            interchange=interchange))
    for item in advice.invoices:
        # Several CUX in one group, as the header has several: the first is
        # taken and the rest are said out loud rather than dropped (#325).
        others = [currency for currency in item.group_currencies
                  if currency != item.group_currency]
        if others:
            out.append(BusinessFinding(
                rule=CURRENCY_DISAGREES, kind=kind, code=message.code,
                control=message.control, po_number="",
                expected=item.group_currency or "",
                found=", ".join(sorted(set(others))),
                note="invoice %s's group names %s; %s taken as the "
                     "document's, being the first, and D.96A does not say "
                     "they must agree"
                     % (item.invoice,
                        # In the order the document gave them, not sorted:
                        # the reason stated here is "the first", so a list
                        # that did not start with it would read as a
                        # contradiction. The header's says "the reference
                        # currency" and can sort.
                        ", ".join(item.group_currencies),
                        item.group_currency),
                interchange=interchange))
        # Several MOA+12 in one group. The first is what was remitted and
        # the rest are reported; nothing is summed, because D.96A does not
        # say two amounts in one group add up and guessing that they do is
        # how the mock would quietly change what a payer said (#325).
        #
        # Reported even where they are equal, and named `repeated` rather
        # than `disagrees` for that reason: two equal currencies say the
        # same thing twice and are harmless, but two equal amounts are
        # ambiguous about whether one payment was stated twice or two were
        # made. That ambiguity is the thing worth saying.
        if len(item.amounts) > 1:
            out.append(BusinessFinding(
                rule=AMOUNTS_REPEATED, kind=kind, code=message.code,
                control=message.control, po_number="",
                expected=str(item.amounts[0]),
                found=", ".join(str(amount) for amount in item.amounts[1:]),
                note="invoice %s's group states %s as the amount remitted; "
                     "%s taken, being the first, and the %s not added to it"
                     % (item.invoice,
                        " and ".join(str(a) for a in item.amounts),
                        item.amounts[0],
                        "other" if len(item.amounts) == 2 else "others"),
                interchange=interchange))
        # The same disagreement one level down, and reported by the same
        # rule: D.96A lets a DOC group state its currency on its CUX and on
        # its MOA, and says no more about it there than it does at the head
        # (#322). Before the early return below, as the header's two are,
        # so that an advice stating a currency only inside a group is still
        # read for this.
        if (item.group_currency and item.currency
                and item.group_currency != item.currency):
            out.append(BusinessFinding(
                rule=CURRENCY_DISAGREES, kind=kind, code=message.code,
                control=message.control, po_number="",
                expected=item.group_currency, found=item.currency,
                note="the CUX in invoice %s's group says %s and its MOA "
                     "says %s; D.96A lets both state one and does not say "
                     "which wins"
                     % (item.invoice, item.group_currency, item.currency),
                interchange=interchange))
    if not advice.currency and not any(item.currency
                                       for item in advice.invoices):
        return out
    for item in advice.invoices:
        if not item.invoice:
            continue
        row = db.one(conn, "SELECT currency FROM invoice WHERE invoice_number = ?"
                           " AND partner = ?", (item.invoice, partner["id"]))
        if row is None or not row["currency"]:
            continue
        # The document's own currency where it states one (D.96A's SG5),
        # the advice's otherwise. An advice in EUR paying one invoice in EUR
        # and saying USD over another is now two different comparisons, which
        # is the point of reading SG5 at all (#298).
        stated = item.currency or advice.currency
        if stated and row["currency"] != stated:
            out.append(BusinessFinding(
                rule=CURRENCY_NOT_THE_INVOICE, kind=kind, code=message.code,
                control=message.control, po_number="",
                expected=row["currency"], found=stated,
                note="%s is in %s and invoice %s is in %s, so the amounts "
                     "are not comparable"
                     % ("the advice" if item.currency is None
                        else "the entry for invoice %s" % item.invoice,
                        stated, item.invoice, row["currency"]),
                interchange=interchange))
    return out


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
            "currency": advice.currency,
            "creditDebit": advice.credit_debit,
            "settles": advice.settles.isoformat() if advice.settles else "",
            "settledOnArrival": (None if advice.settles is None
                                 else not _was_early(conn, row)),
            "invoices": [{"invoice": item.invoice,
                          "paid": None if item.paid is None else str(item.paid),
                          "currency": item.currency or advice.currency}
                         for item in advice.invoices],
            "at": row["at"], "status": "reversal" if advice.credit_debit == DEBIT
                                        else "advised", "reversedBy": None,
            # EANCOM's REMADV correction: BGM 1225 code 5 naming an earlier
            # advice in RFF+RA. Both are needed - an RFF+RA on an original
            # advice is a reference to another advice, not a claim to
            # replace it - so the purpose gates the relation here exactly
            # as it gates the finding. `Advice.replaces` stays faithful to
            # the document either way; this field means "the advice this
            # one replaces", and `replacedBy` is filled in below on the one
            # it named (#187).
            "replaces": (advice.replaces or None
                         if advice.purpose == REPLACEMENT else None),
            "replacedBy": None,
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
    for index, item in enumerate(out):
        if not item["replaces"]:
            continue
        # The most recent one of that number not already replaced, which is
        # what makes a chain work: A replaced by B replaced by C leaves each
        # naming its own predecessor rather than all of them naming A.
        for earlier in reversed(out[:index]):
            if (earlier["partner"], earlier["trace"]) == (item["partner"],
                                                          item["replaces"]) \
                    and earlier["status"] != "replaced":
                earlier["status"] = "replaced"
                earlier["replacedBy"] = item["id"]
                break
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
        taken = False
        for item in message.segments:
            if item.tag in ("DOC", "AJT", "UNS"):
                within = item.tag
                taken = False
            elif item.tag == "MOA" and item.comp(1, 1) == "12":
                if within == "DOC" and not item.comp(1, 2).strip():
                    continue        # states no amount, as `read` has it
                amount = _amount(item.comp(1, 2))
                if amount is None:
                    return None
                if within == "DOC":
                    # The first of a group, as `read` takes it (#325). This
                    # loop used to add every one, so a group with two of
                    # them balanced an advice whose listing showed one: the
                    # arithmetic certified a sum the mock does not make.
                    if not taken:
                        per_document.append(amount)
                        taken = True
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
