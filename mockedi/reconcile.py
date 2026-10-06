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
from typing import Any, Callable, Dict, List, Optional

from . import db, edifact, schema, x12
from .envelope import EdiSyntaxError, Message, Seg

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
        results = _read_contrl(message, interchange_control,
                               sent_by(conn, partner))
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
    segment = ""             # the AK3 an AK4 belongs to

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
            notes, segment = [], ""
        elif item.tag == "AK3" and current is not None:
            segment = item.get(1)
            notes.append("%s at segment %s: %s"
                         % (item.get(1), item.get(2) or "?",
                            _segment_error(item.get(4))))
        elif item.tag == "AK4" and current is not None:
            # No leading indent: these are joined inline with "; ", so the
            # two spaces that would indent a nested line just doubled up the
            # separator in the note a user reads.
            #
            # Named the way a partner's guide writes it - PO103 - from the
            # AK3 above, with the number the 997 gave beside it for whoever
            # is looking for it in the bytes (#294).
            position, component = item.comp(1, 1), item.comp(1, 2)
            numbers = position + (":" + component if component else "")
            notes.append("%s: %s%s"
                         % (_named(segment, position, component, numbers),
                            _element_error(item.get(3)),
                            " (%r)" % item.get(4) if item.has(4) else ""))
        elif item.tag == "AK5" and current is not None:
            current.verdict = item.get(1)
            current.status = X12_VERDICTS.get(item.get(1), ACCEPTED_WITH_ERRORS)
            current.note = "; ".join(notes)
            out.append(current)
            current = None
    return out


def _read_contrl(message: Message, interchange_control: str,
                 sent: Optional[Callable[[str, str, str], Optional[Message]]] = None
                 ) -> List[Matched]:
    """Walk UCI / UCM / UCS / UCD.

    The interchange being acknowledged is named in UCI01, not by the envelope
    this CONTRL arrived in - so a CONTRL is matched against the interchange it
    *quotes*, which is the one the mock sent.

    A CONTRL says *where* a fault is by number and never by name: `UCS`
    carries the segment's position in the message, not its tag, where a 997's
    `AK3` carries the tag. So naming the element needs the message the CONTRL
    is about, and `sent` fetches it - by set code, message reference and the
    interchange quoted. Without it, or for a message the mock never sent,
    the note keeps the numbers (#294).
    """
    out: List[Matched] = []
    acknowledged_interchange = interchange_control
    current: Optional[Matched] = None
    notes: List[str] = []
    about: Optional[Message] = None      # the message a UCM is about
    segment: Optional[Seg] = None        # the segment a UCS points at
    where = ""                           # and the number it gave
    envelope = ""                        # what a UCI said was wrong with it

    verdict = ""
    for item in message.segments:
        if item.tag == "UCI":
            acknowledged_interchange = item.get(1)
            verdict = item.get(4)
            if item.get(6) and item.comp(7, 1):
                # A refused envelope: UCI06 is the service segment's tag, so
                # this one is named with nothing looked up.
                envelope = "%s: %s" % (
                    _named_edifact(None, item.get(6), item.comp(7, 1),
                                   item.comp(7, 2), ""),
                    _edifact_error(item.get(5)))
        elif item.tag == "UCM":
            if current is not None:
                current.note = "; ".join(notes)
                out.append(current)
            current = Matched(code=item.comp(2, 1), control=item.get(1),
                              group_control=acknowledged_interchange,
                              verdict=item.get(3),
                              status=EDIFACT_VERDICTS.get(item.get(3), ACCEPTED))
            notes, segment, where = [], None, ""
            about = sent(current.code, current.control,
                         acknowledged_interchange) if sent else None
        elif item.tag == "UCS" and current is not None:
            where = item.get(1)
            segment = _segment_at(about, where)
            notes.append("%ssegment %s: %s"
                         % ("%s at " % segment.tag if segment is not None else "",
                            where, _edifact_error(item.get(2))))
        elif item.tag == "UCD" and current is not None:
            notes.append("%s: %s" % (
                _named_edifact(about, segment.tag if segment is not None else "",
                               item.comp(2, 1), item.comp(2, 2), where),
                _edifact_error(item.get(1))))
    if current is not None:
        current.note = "; ".join(notes)
        out.append(current)
    if not out and verdict:
        # UCI and no UCM: 0083 answers for every message in the interchange.
        out.append(Matched(
            code="", control="", group_control=acknowledged_interchange,
            verdict=verdict, status=EDIFACT_VERDICTS.get(verdict, ACCEPTED),
            note="acknowledged for the whole interchange by UCI, with no UCM"
                 + ("; %s" % envelope if envelope else ""),
            covers=INTERCHANGE))
    return out


def sent_by(conn: sqlite3.Connection, partner: str, direction: str = "out"
            ) -> Callable[[str, str, str], Optional[Message]]:
    """A way to fetch the message a CONTRL is about, for `_read_contrl`.

    By set code and message reference within the interchange the CONTRL
    quotes - the same three things `_find` matches a CONTRL to a document by,
    so the message that is named from is the one the verdict is recorded
    against. `direction` is which way that message went: `out` for a
    partner's CONTRL about something the mock sent, `in` for the mock's own
    CONTRL about something it received.
    """
    def fetch(code: str, control: str, interchange_control: str
              ) -> Optional[Message]:
        row = db.one(
            conn,
            "SELECT i.payload FROM transaction_set t JOIN interchange i"
            " ON i.id = t.interchange_id"
            " WHERE t.direction = ? AND t.partner = ? AND t.code = ?"
            " AND t.control = ? AND i.control = ? ORDER BY t.id DESC LIMIT 1",
            (direction, partner, code, control, interchange_control))
        if row is None or not row["payload"]:
            return None
        try:
            interchange = edifact.parse(row["payload"])
        except EdiSyntaxError:
            return None
        return next((item for _group, item in interchange.messages()
                     if item.code == code and item.control == control), None)
    return fetch


def _segment_at(about: Optional[Message], where: str) -> Optional[Seg]:
    """The segment 0096 points at: the UNH is 1."""
    if about is None or not where.isdigit():
        return None
    index = int(where) - 1
    return about.segments[index] if 0 <= index < len(about.segments) else None


def _named(tag: str, position: str, component: str, numbers: str) -> str:
    """`PO103 (element 3)`, or the number alone where there is no tag to name."""
    if not tag or not position.isdigit() or int(position) < 1:
        return "element %s" % numbers
    label = "%s%02d" % (tag, int(position))
    if component:
        # Said as a position, in words: `/1` would read as an element's
        # number, which is how a component is named where its number is known.
        label += " component %s" % component
    return "%s (element %s)" % (label, numbers)


def _named_edifact(about: Optional[Message], tag: str, position: str,
                   component: str, where: str) -> str:
    """`QTY01/6060 (segment 4, element 2:2)`: the name, and what the CONTRL said.

    0098 counts the segment tag as position 1 and the dictionary's labels do
    not, so the label is one less. A component is named by its own number in
    the directory - 6060 - which is how the mock's own findings write it,
    and by its position where the dictionary does not know the segment.
    """
    numbers = position + (":" + component if component else "")
    said = ("segment %s, " % where if where else "") + "element %s" % numbers
    if not tag or not position.isdigit() or int(position) < 2:
        return said
    place = int(position) - 1
    label = "%s%02d" % (tag, place)
    if component:
        ref = ""
        definition = (schema.lookup("EDIFACT", about.code)
                      if about is not None else None)
        declared = definition.segment_for(tag) if definition is not None else None
        element = declared.element(place) if declared is not None else None
        if (element is not None and component.isdigit()
                and 1 <= int(component) <= len(element.components)):
            ref = element.components[int(component) - 1].ref
        # By its own number where the dictionary has it; otherwise by its
        # position, in words, so that `9` is not read as a directory number.
        label += "/%s" % ref if ref else " component %s" % component
    return "%s (%s)" % (label, said)


def answers(dialect: str, kind: str, control: str, reference: str,
            payload: str,
            sent: Optional[Callable[[str, str, str], Optional[Message]]] = None
            ) -> Dict[str, Any]:
    """What an acknowledgment answers, read from the acknowledgment itself.

    For the timeline (#197): the envelope, the acknowledgment's own verdict
    on all of it - AK901, the UCI's action, or TA104 - and each set it names
    with that set's verdict. Read by the same two functions that reconcile a
    receipt, so the timeline and the reconciliation cannot read one 997 two
    ways. `reference` is the envelope the row was filed under; `payload` the
    interchange the acknowledgment travelled in.

    A TA1 answers an envelope and no set, and so does a 997 of AK1 and AK9
    alone or a CONTRL with no UCM: `sets` is then empty and the verdict is
    the whole of it.
    """
    out: Dict[str, Any] = {"interchange": reference, "verdict": "",
                           "status": "", "sets": []}
    if not payload:
        return out
    try:
        interchange = (x12.parse(payload) if dialect == "X12"
                       else edifact.parse(payload))
    except EdiSyntaxError:
        return out
    if kind == schema.INTERCHANGE_ACKNOWLEDGMENT:
        # A TA1 sits between ISA and IEA with no group round it, which is
        # where the parser keeps it.
        ta1 = next((item for item in interchange.preamble if item.tag == "TA1"),
                   None)
        verdict = ta1.get(4) if ta1 is not None else ""
        out["verdict"] = verdict
        out["status"] = X12_VERDICTS.get(verdict, "")
        return out
    message = next((item for _group, item in interchange.messages()
                    if item.control == control), None)
    if message is None:
        return out
    if dialect == "X12":
        found = _read_997(message)
        trailer = message.find("AK9")
        verdict = trailer.get(1) if trailer is not None else ""
        out["status"] = X12_VERDICTS.get(verdict, "")
    else:
        found = _read_contrl(message, reference, sent)
        head = message.find("UCI")
        verdict = head.get(4) if head is not None else ""
        out["status"] = EDIFACT_VERDICTS.get(verdict, "")
    out["verdict"] = verdict
    out["sets"] = [{"code": item.code, "control": item.control,
                    "verdict": item.verdict, "status": item.status,
                    "note": item.note}
                   for item in found if not item.covers]
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
    # The acknowledgment's own place in the sequence, not the document's
    # (#195): what happened here is the partner's receipt arriving, which is
    # later than the document it answers and may be later than things the
    # mock sent in between.
    conn.execute(
        "UPDATE transaction_set SET ack_status = ?, ack_code = ?, ack_note = ?,"
        " ack_at = ?, ack_seq = ? WHERE id = ?",
        (result.status, result.verdict, result.note, db.now(conn),
         db.next_seq(conn), row["id"]))
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
        cutoff = (db.moment(conn)
                  - datetime.timedelta(seconds=older_than))
        clauses.append("at <= ?")
        params.append(db.stamp(cutoff))
    params.append(limit)
    return db.rows(conn, "SELECT id, partner, dialect, code, kind, control,"
                         " group_control, reference, at FROM transaction_set"
                         " WHERE %s ORDER BY id DESC LIMIT ?"
                         % " AND ".join(clauses), params)
