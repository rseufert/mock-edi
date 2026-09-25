"""Reading an acknowledgment for something the mock sent.

The mock has always *sent* 997s and CONTRLs.  Until now it never read one:
an acknowledgment that arrived was parsed, validated and filed like any other
document, and then nothing happened to it.  That left the failure most worth
testing - *my invoice was never acknowledged* - impossible to reproduce,
because the mock had no notion of a document still waiting for a receipt.

Matching an acknowledgment to what it acknowledges is the whole job, and the
two dialects address the thing they are acknowledging differently:

* **X12** addresses a *transaction set inside a functional group*.  `AK102`
  quotes GS06, `AK202` quotes ST02, and `AK501` is the verdict for that set.
  Both numbers are needed: ST02 is only unique within its group.
* **EDIFACT** addresses a *message inside an interchange*.  `UCI01` quotes
  UNB's control reference and `UCM01` quotes UNH01, with the action code in
  `0083` at each level.

Both may also answer for everything at once, and most translators do when
nothing was wrong: a 997 of `AK1` and `AK9` alone, with no `AK2` loop, is the
commonest shape in the wild, and a CONTRL may carry `UCI` and no `UCM`.  That
verdict applies to every set in the group, or every message in the
interchange, that the acknowledgment names.

An acknowledgment naming something the mock never sent is recorded as
unmatched rather than dropped.  It is a real and common condition - a
duplicate, a receipt for a document that was never delivered, or a partner
quoting the wrong control number - and silently ignoring it would hide the
bug it is evidence of.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from . import db, schema
from .envelope import Message

# What we record against the document that was acknowledged.
# Kinds that answer a document rather than await an answer.
ACKNOWLEDGMENT_KINDS = (schema.ACKNOWLEDGMENT, schema.INTERCHANGE_ACKNOWLEDGMENT)

ACCEPTED = "accepted"
ACCEPTED_WITH_ERRORS = "accepted-with-errors"
REJECTED = "rejected"

# X12 717 (AK501) and 715 (AK901), which share their vocabulary.
X12_VERDICTS = {
    "A": ACCEPTED,
    "E": ACCEPTED_WITH_ERRORS,
    "P": ACCEPTED_WITH_ERRORS,
    "R": REJECTED,
    "M": REJECTED,
    "W": REJECTED,
    "X": REJECTED,
}
# EDIFACT 0083, in CONTRL.
EDIFACT_VERDICTS = {"7": ACCEPTED, "8": ACCEPTED, "4": REJECTED, "2": REJECTED}


@dataclass
class Matched:
    """One transaction set an acknowledgment had something to say about."""
    code: str
    control: str
    group_control: str = ""
    status: str = ""
    verdict: str = ""
    note: str = ""
    document_id: int = 0
    reference: str = ""
    # Set when the verdict is for a whole group (the AK101 functional id) or
    # a whole interchange, rather than for one set; `code` and `control` are
    # then empty until it is matched to the sets it covers.
    covers: str = ""

    @property
    def matched(self) -> bool:
        return bool(self.document_id)


GROUP = "group"
INTERCHANGE = "interchange"


def apply(conn: sqlite3.Connection, partner: str, dialect: str,
          message: Message, interchange_control: str = "") -> List[Matched]:
    """Record an inbound acknowledgment against the documents it acknowledges."""
    if dialect == "X12":
        results = _read_997(message)
    else:
        results = _read_contrl(message, interchange_control)
    out: List[Matched] = []
    for result in results:
        for item in _expand(conn, partner, dialect, result) if result.covers \
                else [result]:
            _record(conn, partner, dialect, item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _read_997(message: Message) -> List[Matched]:
    """Walk AK1 / AK2 / AK3 / AK4 / AK5, gathering a verdict per transaction set."""
    out: List[Matched] = []
    group_control = ""
    current: Optional[Matched] = None
    notes: List[str] = []

    functional_id = ""
    loops = 0

    for item in message.segments:
        if item.tag == "AK1":
            functional_id, group_control, loops = item.get(1), item.get(2), 0
        elif item.tag == "AK9" and not loops:
            # AK1 and AK9 alone: AK901 is the verdict on every set in the
            # group, which is how most translators say "all accepted".
            out.append(Matched(
                code="", control="", group_control=group_control,
                verdict=item.get(1),
                status=X12_VERDICTS.get(item.get(1), ACCEPTED_WITH_ERRORS),
                note="acknowledged for the whole group by AK9, with no AK2 loop",
                covers=functional_id or GROUP))
        elif item.tag == "AK2":
            loops += 1
            current = Matched(code=item.get(1), control=item.get(2),
                              group_control=group_control)
            notes = []
        elif item.tag == "AK3" and current is not None:
            notes.append("%s at segment %s: %s"
                         % (item.get(1), item.get(2) or "?",
                            _segment_error(item.get(4))))
        elif item.tag == "AK4" and current is not None:
            # No leading indent: these are joined inline with "; ", so the
            # two spaces that would indent a nested line just doubled up the
            # separator in the note a user reads.
            notes.append("element %s: %s%s"
                         % (item.get(1), _element_error(item.get(3)),
                            " (%r)" % item.get(4) if item.has(4) else ""))
        elif item.tag == "AK5" and current is not None:
            current.verdict = item.get(1)
            current.status = X12_VERDICTS.get(item.get(1), ACCEPTED_WITH_ERRORS)
            current.note = "; ".join(notes)
            out.append(current)
            current = None
    return out


def _read_contrl(message: Message, interchange_control: str) -> List[Matched]:
    """Walk UCI / UCM / UCS / UCD.

    The interchange being acknowledged is named in UCI01, not by the envelope
    this CONTRL arrived in - so a CONTRL is matched against the interchange it
    *quotes*, which is the one the mock sent.
    """
    out: List[Matched] = []
    acknowledged_interchange = interchange_control
    current: Optional[Matched] = None
    notes: List[str] = []

    verdict = ""
    for item in message.segments:
        if item.tag == "UCI":
            acknowledged_interchange = item.get(1)
            verdict = item.get(4)
        elif item.tag == "UCM":
            if current is not None:
                current.note = "; ".join(notes)
                out.append(current)
            current = Matched(code=item.comp(2, 1), control=item.get(1),
                              group_control=acknowledged_interchange,
                              verdict=item.get(3),
                              status=EDIFACT_VERDICTS.get(item.get(3), ACCEPTED))
            notes = []
        elif item.tag == "UCS" and current is not None:
            notes.append("segment %s: %s"
                         % (item.get(1), _edifact_error(item.get(2))))
        elif item.tag == "UCD" and current is not None:
            notes.append("element %s: %s"
                         % (item.comp(2, 1), _edifact_error(item.get(1))))
    if current is not None:
        current.note = "; ".join(notes)
        out.append(current)
    if not out and verdict:
        # UCI and no UCM: 0083 answers for every message in the interchange.
        out.append(Matched(
            code="", control="", group_control=acknowledged_interchange,
            verdict=verdict, status=EDIFACT_VERDICTS.get(verdict, ACCEPTED),
            note="acknowledged for the whole interchange by UCI, with no UCM",
            covers=INTERCHANGE))
    return out


def _segment_error(code: str) -> str:
    from . import schema
    return schema.SEGMENT_ERROR_CODES.get(code, code or "no detail")


def _element_error(code: str) -> str:
    from . import schema
    return schema.ELEMENT_ERROR_CODES.get(code, code or "no detail")


def _edifact_error(code: str) -> str:
    from . import schema
    return schema.EDIFACT_SYNTAX_ERRORS.get(code, code or "no detail")


# ---------------------------------------------------------------------------
# Matching and recording
# ---------------------------------------------------------------------------

def _record(conn: sqlite3.Connection, partner: str, dialect: str,
            result: Matched) -> None:
    row = _find(conn, partner, dialect, result)
    if row is None:
        return
    result.document_id = int(row["id"])
    result.reference = row["reference"]
    conn.execute(
        "UPDATE transaction_set SET ack_status = ?, ack_code = ?, ack_note = ?,"
        " ack_at = ? WHERE id = ?",
        (result.status, result.verdict, result.note, db.now(), row["id"]))
    conn.commit()


def _expand(conn: sqlite3.Connection, partner: str, dialect: str,
            result: Matched) -> List[Matched]:
    """A verdict for a whole group or interchange, as one per set it covers.

    A 997's AK101 names the functional group, so only the sets that belong
    to it are covered: an `AK1*PR` group answers 855s, not the 856 that went
    out with the same group control number to someone else's group. What
    the mock sent in answer to the partner - its own acknowledgments - is
    never covered. With nothing to cover, the verdict is kept, unmatched.
    """
    answers = tuple(ACKNOWLEDGMENT_KINDS)
    if dialect == "X12":
        codes = [code for code, definition in schema.X12_SETS.items()
                 if definition.group == result.covers]
        if not codes:
            return [result]
        rows = db.rows(
            conn,
            "SELECT * FROM transaction_set WHERE direction = 'out'"
            " AND partner = ? AND group_control = ? AND code IN (%s)"
            " ORDER BY id" % ", ".join("?" for _ in codes),
            [partner, result.group_control] + codes)
    else:
        rows = db.rows(
            conn,
            "SELECT t.* FROM transaction_set t JOIN interchange i"
            " ON i.id = t.interchange_id"
            " WHERE t.direction = 'out' AND t.partner = ? AND i.control = ?"
            " AND t.kind NOT IN (%s) ORDER BY t.id"
            % ", ".join("?" for _ in answers),
            [partner, result.group_control] + list(answers))
    if not rows:
        return [result]
    return [Matched(code=row["code"], control=row["control"],
                    group_control=result.group_control, status=result.status,
                    verdict=result.verdict, note=result.note)
            for row in rows]


def _find(conn: sqlite3.Connection, partner: str, dialect: str,
          result: Matched) -> Optional[Dict[str, Any]]:
    """The outbound transaction set an acknowledgment is about, if we sent one.

    X12 matches on the pair of control numbers, because ST02 is only unique
    within its functional group.  EDIFACT matches the message reference within
    the interchange the CONTRL quotes, which means joining to the interchange
    the document went out in.
    """
    if dialect == "X12":
        return db.one(
            conn,
            "SELECT * FROM transaction_set WHERE direction = 'out'"
            " AND partner = ? AND code = ? AND control = ? AND group_control = ?"
            " ORDER BY id DESC LIMIT 1",
            (partner, result.code, result.control, result.group_control))
    return db.one(
        conn,
        "SELECT t.* FROM transaction_set t JOIN interchange i"
        " ON i.id = t.interchange_id"
        " WHERE t.direction = 'out' AND t.partner = ? AND t.code = ?"
        " AND t.control = ? AND i.control = ?"
        " ORDER BY t.id DESC LIMIT 1",
        (partner, result.code, result.control, result.group_control))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def unacknowledged(conn: sqlite3.Connection, older_than: float = 0.0,
                   partner: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    """Documents the mock sent that nobody has acknowledged.

    `older_than` is in seconds, and is the question an operations team
    actually asks: not "what is outstanding" but "what has been outstanding
    long enough to chase".
    """
    import datetime
    clauses = ["direction = 'out'", "ack_status = ''",
               # No acknowledgment is itself acknowledged - neither a 997 nor
               # a TA1 - so neither is ever outstanding.
               "kind NOT IN (%s)"
               % ", ".join("'%s'" % k for k in ACKNOWLEDGMENT_KINDS)]
    params: List[Any] = []
    if partner:
        clauses.append("partner = ?")
        params.append(partner)
    if older_than:
        cutoff = (datetime.datetime.now()
                  - datetime.timedelta(seconds=older_than))
        clauses.append("at <= ?")
        params.append(cutoff.replace(microsecond=0).isoformat())
    params.append(limit)
    return db.rows(conn, "SELECT id, partner, dialect, code, kind, control,"
                         " group_control, reference, at FROM transaction_set"
                         " WHERE %s ORDER BY id DESC LIMIT ?"
                         % " AND ".join(clauses), params)
