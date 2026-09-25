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
from .validate import InterchangeReport, MessageReport

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


# ---------------------------------------------------------------------------
# X12 997
# ---------------------------------------------------------------------------

def functional_acknowledgment(functional_id: str, group_control: str,
                              version: str,
                              messages: Sequence[MessageReport],
                              group_errors: Sequence[Tuple[str, str]] = (),
                              carries_version: bool = False) -> List[Seg]:
    """The body of a 997, acknowledging one functional group.

    AK9's three counts are the part receivers actually check: transaction sets
    included in this acknowledgment, received in the group, and accepted.  When
    they disagree with the 850s that were sent, someone's envelope is wrong.

    `group_errors` are the group's own faults - a missing GE, a count or
    control number that does not match - and go in AK905 onward. Any of them
    rejects the whole group, however clean the sets inside it were.
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
        out.append(_ak5(item))
        if item.accepted:
            accepted += 1

    verdict = "R" if group_errors else _group_code(messages)
    out.append(seg("AK9", verdict, str(len(messages)), str(len(messages)),
                   str(accepted), *[code for code, _note in group_errors[:5]]))
    return out


def _ak5(item: MessageReport) -> Seg:
    if item.clean:
        return seg("AK5", "A")
    code = "A" if item.accepted and not item.set_errors else (
        "E" if item.accepted else "R")
    elements: List[str] = [code]
    for error, _note in item.set_errors[:5]:
        elements.append(error)
    return seg("AK5", *elements)


def _group_code(messages: Sequence[MessageReport]) -> str:
    if not messages:
        return "R"
    accepted = sum(1 for m in messages if m.accepted)
    if all(m.clean for m in messages):
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
    """AK404 carries a copy of the bad data, and is 99 characters at most."""
    text = (value or "").strip()
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
                    [str(finding.position)] if finding.position else "")]
    out: List[Seg] = [seg(
        "UCI", interchange.control, sender, receiver,
        REJECTED if not any(m.accepted for m in messages) else ACKNOWLEDGED,
    )]
    for item in messages:
        # The version the sender declared in its own UNH, not ours.
        version = (item.version or item.group_version or "D:96A:UN").split(":")
        while len(version) < 3:
            version.append("UN")
        out.append(seg(
            "UCM", item.control, [item.code] + version[:3],
            ACKNOWLEDGED if item.accepted else REJECTED,
            "" if item.clean else _worst(item),
        ))
        for finding in item.segments:
            out.append(seg("UCS", str(max(1, finding.position)),
                           finding.edifact_code))
            for element in finding.elements:
                out.append(seg("UCD", element.edifact_code,
                               [str(element.position),
                                str(element.component) if element.component else ""]))
    return out


def _worst(item: MessageReport) -> str:
    """The one syntax error code UCM carries for the message as a whole."""
    for finding in item.segments:
        if finding.severity == "fatal":
            return finding.edifact_code
    return item.segments[0].edifact_code if item.segments else "12"


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
    return lines
