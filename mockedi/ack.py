"""Turning a validation report into an acknowledgment.

Both dialects have a message whose only job is to say "your syntax parsed" or
"it did not, here is where".  X12 calls it the 997 functional acknowledgment;
EDIFACT calls it CONTRL.  Neither says anything about whether the business
accepted the order - that is the 855 or the ORDRSP, and conflating the two is
the most common misreading of EDI there is.  A 997 with AK501 = A means the
file was well formed.  It does not mean anyone will ship anything.

One report renders into either shape, because `validate.py` produced findings
rather than prose.  The X12 codes are the primary ones; the EDIFACT codes are
the nearest equivalent, which is all a translation between the two can be.
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from .envelope import Interchange, Seg, seg
from .validate import SET_ERRORS_AS_0085, InterchangeReport, MessageReport

# The 997 acknowledges one functional group, so an interchange carrying two
# groups is answered with two of them.
GroupReports = List[Tuple[str, str, str, List[MessageReport]]]


def group_reports(interchange: Interchange,
                  report: InterchangeReport) -> GroupReports:
    """Every functional group in the envelope, with its messages' reports.

    Gathered from the *envelope* rather than from the reports, so that a group
    carrying no transaction set still gets a 997 of its own.  It was sent, and
    a receiver that says nothing about it cannot be told apart from one that
    never received it - which is the outcome `docs/ARCHITECTURE.md` rules out.

    The implicit group - the one the parser invents around a transaction set
    that arrived outside any GS - is left out.  There is no GS to answer, and
    answering anyway wrote `AK1*??*0`, which the mock's own dictionary
    rejects.  That shape is refused at interchange level instead.
    """
    buckets: Dict[Tuple[str, str], List[MessageReport]] = {}
    for item in report.messages:
        buckets.setdefault((item.group_id, item.group_control), []).append(item)
    return [(group.functional_id, group.control, group.version,
             buckets.get((group.functional_id, group.control), []))
            for group in interchange.groups if not group.implicit]


def trailer_counts(interchange: Interchange) -> Dict[Tuple[str, str], str]:
    """What each group's own trailer said it held: GE01, by (GS01, GS06).

    Only a count that can be quoted. AK902 is numeric and mandatory, so a
    group with no GE, or a GE01 that is empty, not digits or longer than the
    six an element 97 may be, is left out, and the 997 falls back to the
    number of sets that arrived rather than carry something invalid itself.
    """
    counts: Dict[Tuple[str, str], str] = {}
    for group in interchange.groups:
        if group.implicit or group.trailer is None:
            continue
        said = (group.trailer.get(1) or "").strip()
        if said.isdigit() and len(said) <= 6:
            counts[(group.functional_id, group.control)] = str(int(said))
    return counts


# ---------------------------------------------------------------------------
# X12 997
# ---------------------------------------------------------------------------

def functional_acknowledgment(functional_id: str, group_control: str,
                              version: str,
                              messages: Sequence[MessageReport],
                              group_errors: Sequence[Tuple[str, str]] = (),
                              carries_version: bool = False,
                              included: str = "") -> List[Seg]:
    """The body of a 997, acknowledging one functional group.

    AK9's three counts are the part receivers actually check: transaction sets
    the sender's trailer said the group held, received in it, and accepted.
    When they disagree with the 850s that were sent, someone's envelope is
    wrong. `included` is that trailer count, GE01, from `trailer_counts`; with
    none to quote, AK902 is the number received (#221).

    `group_errors` are the group's own faults - a missing GE, a count or
    control number that does not match - and go in AK905 onward. Any of them
    rejects the whole group, however clean the sets inside it were, and then
    every AK5 says R too: a reader that takes its verdict from AK5 would
    otherwise see `AK5*A` for an order that was dropped (#205). The loops
    could be left out instead - the guides allow it - but a reader that
    applies AK9 to the group only when there is no AK2 loop at all would then
    learn nothing about a clean set sharing a group with a flawed one.
    """
    # AK103 exists from 005010 on: a 997 at 004010 names the group and its
    # control number, and nothing more.
    ak1 = [functional_id or "??", _digits(group_control)]
    if carries_version:
        ak1.append(version or "")
    out: List[Seg] = [seg("AK1", *ak1)]
    accepted = 0
    for item in messages:
        out.append(seg("AK2", item.code, item.control))
        for finding in item.segments:
            out.append(seg("AK3", finding.tag, str(max(1, finding.position)),
                           finding.loop, finding.code))
            for element in finding.elements:
                out.append(seg("AK4",
                               _position(element),
                               element.ref, element.code,
                               _clip(element.value)))
        out.append(_ak5(item, group_rejected=bool(group_errors)))
        if item.accepted and not group_errors:
            accepted += 1

    verdict = "R" if group_errors else _group_code(messages)
    out.append(seg("AK9", verdict, included or str(len(messages)),
                   str(len(messages)),
                   str(accepted), *[code for code, _note in group_errors[:5]]))
    return out


def _ak5(item: MessageReport, group_rejected: bool = False) -> Seg:
    if item.refused:
        # A translator that rejects what it should not gives no reason.
        return seg("AK5", "R")
    if item.clean:
        # In a rejected group there is nothing of the set's own to name in
        # AK502: the reason is the group's, and it is in AK905.
        return seg("AK5", "R" if group_rejected else "A")
    code = "A" if item.accepted and not item.set_errors else (
        "E" if item.accepted else "R")
    if group_rejected:
        # Neither "accepted" nor "accepted with errors" can be said of a set
        # inside a group that was rejected. What was wrong with it stays.
        code = "R"
    elements: List[str] = [code]
    for error, _note in item.set_errors[:5]:
        elements.append(error)
    return seg("AK5", *elements)


def _group_code(messages: Sequence[MessageReport]) -> str:
    if not messages:
        return "R"
    accepted = sum(1 for m in messages if m.accepted)
    if all(m.clean and not m.refused for m in messages):
        return "A"
    if accepted == 0:
        return "R"
    if accepted < len(messages):
        return "P"
    return "E"


def _position(element) -> str:
    """AK401 is the element's position; a composite adds its component position.

    X12 writes that as a composite of its own, C030, so `2>1` is component 1
    of element 2 - which is how an EDIFACT document converted to X12 reports a
    bad component.
    """
    if element.component:
        return "%d>%d" % (element.position, element.component)
    return str(element.position)


def _clip(value: str, limit: int = 99) -> str:
    """AK404 carries a copy of the bad data, and is 99 characters at most.

    Unless the copy would itself be a syntax error, which the segment's own
    semantic note rules out: a control character is not allowed in a data
    element in any X12 character set, so a 997 that quoted one back could be
    rejected for the fault it was reporting (#231). The element is then left
    out whole rather than tidied - a copy with the character removed is not
    a copy - and AK401 to AK403 still say which element and why. A letter
    outside ASCII is kept: it is legal in the extended set, and the 997 goes
    out in the character set the document came in.
    """
    text = (value or "").strip()
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value or ""):
        return ""
    return text[:limit]


def _digits(value: str) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit()) or "0"


# ---------------------------------------------------------------------------
# EDIFACT CONTRL
# ---------------------------------------------------------------------------

ACKNOWLEDGED = "7"
REJECTED = "4"


def syntax_report(interchange: Interchange, report: InterchangeReport,
                  messages: Sequence[MessageReport]) -> List[Seg]:
    """The body of a CONTRL, acknowledging one interchange.

    UCI names the interchange being acknowledged - its control reference and
    *its* sender and recipient, not this message's - which is why a CONTRL
    looks, at first glance, as though the addresses are the wrong way round.
    """
    sender = [interchange.sender, interchange.sender_qualifier]
    receiver = [interchange.receiver, interchange.receiver_qualifier]
    if report.interchange_rejected:
        # The envelope is at fault, so nothing inside it was read: the verdict
        # is UCI's alone, naming the service segment and element, and there
        # is no UCM to give.
        finding = [f for f in report.interchange_findings
                   if f.severity == "fatal"][0]
        return [seg("UCI", interchange.control, sender, receiver, REJECTED,
                    finding.code, finding.tag,
                    [_in_segment(finding.position)]
                    if finding.position else "")]
    # UCI speaks for the interchange as such. A sound UNB carrying messages
    # that were refused is acknowledged at this level, 7, with the refusals in
    # their UCMs; 4 means the interchange itself was at fault. An interchange
    # with nothing in it is that: 32, Lower level empty.
    if report.envelope_errors:
        out: List[Seg] = [seg("UCI", interchange.control, sender, receiver,
                              REJECTED, "32")]
    else:
        out = [seg("UCI", interchange.control, sender, receiver, ACKNOWLEDGED)]
    for item in messages:
        # The version the sender declared in its own UNH, not ours.
        version = (item.version or item.group_version or "D:96A:UN").split(":")
        while len(version) < 3:
            version.append("UN")
        code, service_segment = ("", "") if item.clean else _worst(item)
        out.append(seg(
            "UCM", item.control, [item.code] + version[:3],
            ACKNOWLEDGED if item.accepted else REJECTED,
            code, service_segment,
        ))
        for finding in item.segments:
            out.append(seg("UCS", str(max(1, finding.position)),
                           finding.edifact_code))
            for element in finding.elements:
                out.append(seg("UCD", element.edifact_code,
                               [_in_segment(element.position),
                                str(element.component) if element.component else ""]))
    return out


def _in_segment(position: int) -> str:
    """A data element's position as S011's 0098 counts it.

    One more than the mock's own, because the standard counts the segment tag:
    "The segment code and each following simple or composite data element
    defined in the segment description shall cause the count to be
    incremented. The segment tag has position number 1." So `QTY01` is 2 here
    and `BGM03` is 4 (#208).

    0104 beside it is *not* shifted: "the count starts at 1" for a component,
    with no tag to count, so the second component of C186 is 2 in both
    numberings. The two components of one composite are counted differently
    on purpose, which is the whole reason this has its own function.
    """
    return str(position + 1)


def _worst(item: MessageReport) -> Tuple[str, str]:
    """The one syntax error code UCM carries for the message as a whole, and
    the service segment it is about when it is about one.

    A fault in the message's own header or trailer outranks anything inside
    it; then the first fatal finding; then the first finding at all. A
    finding with element detail is reported by the element's code - 39 for
    too long, not 12 - since that is what the UCD beneath it says too.
    """
    for code, _note in item.set_errors:
        if code in SET_ERRORS_AS_0085:
            return SET_ERRORS_AS_0085[code]
    fatal = [f for f in item.segments if f.severity == "fatal"]
    for finding in fatal + list(item.segments):
        elements = ([e for e in finding.elements if e.severity == "fatal"]
                    or finding.elements)
        return (elements[0].edifact_code if elements else finding.edifact_code), ""
    return "12", ""


# ---------------------------------------------------------------------------
# X12 TA1
# ---------------------------------------------------------------------------

def interchange_acknowledgment(interchange: Interchange,
                               report: InterchangeReport) -> Seg:
    """A TA1: the verdict on the ISA/IEA envelope, before any group is read.

    TA104 is A, E (accepted, with the fault noted) or R, and TA105 names one
    fault - the standard allows no more - so a rejection names the fault
    that caused it rather than the first one found.
    """
    header = interchange.header
    findings = report.interchange_findings
    fatal = [f for f in findings if f.severity == "fatal"]
    if fatal:
        verdict, note = "R", fatal[0].code
    elif findings:
        verdict, note = "E", findings[0].code
    else:
        verdict, note = "A", "000"
    control = _digits(header.get(13) if header is not None else interchange.control)
    return seg("TA1", control[-9:].rjust(9, "0"),
               _fixed_digits(interchange.date, 6),
               _fixed_digits(interchange.time, 4), verdict, note)


def _fixed_digits(value: str, width: int) -> str:
    """TA102/TA103 are fixed width; a sender's malformed ISA09 is not ours to copy."""
    value = (value or "").strip()
    return value if len(value) == width and value.isdigit() else "0" * width


# ---------------------------------------------------------------------------
# A human-readable rendering, for the control plane and for test failures
# ---------------------------------------------------------------------------

def explain(report: InterchangeReport) -> List[str]:
    """The findings as lines of prose.

    A 997 is precise and unreadable; when a test fails, this is what you want
    to look at.  It is reported by `/_mock/documents` beside the raw payload.
    """
    lines: List[str] = []
    for code, note in report.envelope_errors:
        lines.append("interchange: %s (%s)" % (note, code))
    for finding in report.interchange_findings:
        lines.append("interchange: %s (%s%s)" % (
            finding.note, finding.code,
            "" if finding.severity == "fatal" else ", noted"))
    for (functional_id, control), errors in report.group_errors.items():
        for code, note in errors:
            lines.append("group %s/%s: %s (%s)" % (functional_id or "?", control,
                                                  note, code))
    for item in report.messages:
        if item.envelope_rejected:
            verdict = "rejected with the envelope around it"
        elif item.clean:
            verdict = "accepted"
        elif item.accepted:
            verdict = "accepted, with findings"
        else:
            verdict = "rejected"
        lines.append("%s/%s: %s" % (item.code, item.control, verdict))
        for finding in item.segments:
            where = "%s at segment %d" % (finding.tag, finding.position)
            if finding.loop:
                where += " in the %s loop" % finding.loop
            if finding.elements:
                for element in finding.elements:
                    lines.append("  %s: %s" % (where, element.note))
            else:
                lines.append("  %s: %s" % (where, finding.note))
        for code, note in item.set_errors:
            lines.append("  %s (%s)" % (note, code))
        for finding in item.disagreements:
            # Business, not syntax: said as such, so nobody reads one as the
            # reason a set was rejected.
            # A remittance names no order, and disagrees with itself.
            lines.append("  disagrees%s%s: %s (%s)" % (
                " with the order" if finding.po_number else "",
                " at line %s" % finding.line if finding.line else "",
                finding.note, finding.rule))
    return lines
