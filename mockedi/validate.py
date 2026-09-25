"""Checking a document against the dictionary.

Everything here is derived from `schema.py`.  There is no list of rules: the
rules *are* the dictionary, and a check is a loop over it.  Add a segment to a
transaction set and it is validated; give an element a code list and codes
outside it are reported.

What comes out is a structured report, not prose, because it has to be
rendered twice - as an X12 997 and as an EDIFACT CONTRL - and those two say
the same things with different numbers.  `ack.py` does the rendering.

**Severity.**  The standards say what is wrong, not what to do about it, and
translators differ.  The mock's policy:

* *fatal* rejects the transaction set - an unknown set, a missing mandatory
  segment or element, a control number that does not match its trailer, a
  segment count that does not add up.  These make the document unreliable to
  interpret at all.
* *error* accepts it and says so - an invalid code value, a length violation,
  a malformed date, a segment the set does not define.  These are real
  findings that a receiver can usually work around.

A partner configured `strict` rejects on either.  That is the second most
useful failure to be able to reproduce on demand, after no acknowledgment at
all - and both are things real trading partners do.

**Limits, stated plainly.**  Loop *membership* and repetition counts are
checked; loop *sequence* is not, so a DTM that belongs after the REF and
arrives before it passes.  Conditional requirements ("if PO104 is present then
PO103 must be") are not modelled: the dictionary records M/O, and C is treated
as O.  Both are real parts of X12 that would need a rule language to express,
and the mock would rather leave them out than pretend.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import schema
from .envelope import Interchange, Message, Seg, parse_date

FATAL = "fatal"
ERROR = "error"

_DIGITS = re.compile(r"^-?\d+$")
_DECIMAL = re.compile(r"^-?\d*\.?\d+$")


@dataclass
class ElementFinding:
    """One element of one segment, and what is wrong with it."""
    position: int             # 1-based position in the segment
    component: int            # 1-based component, or 0 when not composite
    ref: str                  # the standard's own number for the element
    code: str                 # X12 723 code
    value: str
    note: str
    severity: str = ERROR

    @property
    def edifact_code(self) -> str:
        """The nearest EDIFACT 0085 code, for a CONTRL."""
        return {"1": "13", "2": "13", "3": "16", "4": "12", "5": "12",
                "6": "12", "7": "12", "8": "12", "9": "12"}.get(self.code, "12")


@dataclass
class SegmentFinding:
    """One segment, and what is wrong with it."""
    tag: str
    position: int             # 1-based position in the message, header is 1
    loop: str = ""
    code: str = ""            # X12 720 code
    note: str = ""
    severity: str = ERROR
    elements: List[ElementFinding] = field(default_factory=list)

    @property
    def edifact_code(self) -> str:
        return {"1": "15", "2": "15", "3": "13", "4": "36", "5": "35",
                "6": "15", "7": "15", "8": "12"}.get(self.code, "12")


@dataclass
class EnvelopeFinding:
    """Something wrong with the interchange envelope rather than a message.

    `code` is the dialect's own: a TA1 note code (I18) for X12, a syntax
    error code (0085) for EDIFACT. The envelope segments differ too much
    between the two for one code to translate into the other.
    """
    code: str
    note: str
    tag: str = ""             # the envelope segment at fault: IEA, UNZ, ISA...
    position: int = 0         # its element, when one element is at fault
    severity: str = FATAL


@dataclass
class MessageReport:
    """The verdict on one transaction set."""
    code: str
    control: str
    kind: str = ""
    version: str = ""
    group_control: str = ""
    group_id: str = ""
    group_version: str = ""
    known: bool = True
    segments: List[SegmentFinding] = field(default_factory=list)
    set_errors: List[Tuple[str, str]] = field(default_factory=list)  # (718 code, note)
    accepted: bool = True
    # Refused because the group or interchange around it was, not because of
    # anything in the set itself; its own findings may be clean.
    envelope_rejected: bool = False

    @property
    def findings(self) -> List[SegmentFinding]:
        return self.segments

    def count(self, severity: str) -> int:
        total = 0
        for finding in self.segments:
            if finding.code and finding.severity == severity:
                total += 1
            total += sum(1 for e in finding.elements if e.severity == severity)
        return total

    @property
    def clean(self) -> bool:
        return not self.segments and not self.set_errors

    def summary(self) -> str:
        if self.clean:
            return "accepted"
        parts = []
        for finding in self.segments:
            for element in finding.elements:
                parts.append("%s%02d: %s" % (finding.tag, element.position, element.note))
            if finding.code and not finding.elements:
                parts.append("%s: %s" % (finding.tag, finding.note))
        for _, note in self.set_errors:
            parts.append(note)
        return "; ".join(parts[:10])


@dataclass
class InterchangeReport:
    """The verdict on a whole interchange."""
    dialect: str
    control: str
    sender: str = ""
    receiver: str = ""
    messages: List[MessageReport] = field(default_factory=list)
    envelope_errors: List[Tuple[str, str]] = field(default_factory=list)
    # (functional id, group control) -> [(716 code, note)], for AK905-AK909.
    group_errors: Dict[Tuple[str, str], List[Tuple[str, str]]] = field(
        default_factory=dict)
    interchange_findings: List[EnvelopeFinding] = field(default_factory=list)

    @property
    def accepted(self) -> int:
        return sum(1 for m in self.messages if m.accepted)

    @property
    def received(self) -> int:
        return len(self.messages)

    @property
    def interchange_rejected(self) -> bool:
        return any(f.severity == FATAL for f in self.interchange_findings)

    @property
    def clean(self) -> bool:
        return (not self.envelope_errors and not self.group_errors
                and not self.interchange_findings
                and all(m.clean for m in self.messages))

    @property
    def group_code(self) -> str:
        """The 997's AK901: accepted, partially accepted, rejected."""
        if self.envelope_errors or self.group_errors or self.interchange_rejected:
            return "R"
        if not self.messages:
            return "R"
        if all(m.clean for m in self.messages):
            return "A"
        if self.accepted == 0:
            return "R"
        if self.accepted < self.received:
            return "P"
        return "E"


def validate(interchange: Interchange, strict: bool = False) -> InterchangeReport:
    """Check every message in an interchange against the dictionary."""
    report = InterchangeReport(dialect=interchange.dialect,
                               control=interchange.control,
                               sender=interchange.sender,
                               receiver=interchange.receiver)
    groups = []
    for group, message in interchange.messages():
        item = validate_message(message, interchange.dialect, strict)
        item.group_control = group.control
        item.group_id = group.functional_id
        item.group_version = group.version
        report.messages.append(item)
        groups.append(group)
    if not report.messages:
        report.envelope_errors.append(
            ("5", "the interchange holds no transaction sets"))

    _check_envelope(interchange, report)
    for group, item in zip(groups, report.messages):
        if (report.interchange_rejected
                or (group.functional_id, group.control) in report.group_errors):
            item.accepted = False
            item.envelope_rejected = True
    return report


def validate_message(message: Message, dialect: str,
                     strict: bool = False) -> MessageReport:
    definition = schema.lookup(dialect, message.code)
    report = MessageReport(code=message.code, control=message.control,
                           version=message.version,
                           kind=schema.kind_of(dialect, message.code))
    if definition is None:
        report.known = False
        report.accepted = False
        report.set_errors.append(
            ("1", "transaction set %s is not one this mock implements" % message.code))
        return report

    _check_trailer(message, dialect, report)
    _check_version(message, dialect, definition, report)
    _walk(message, definition, report)

    fatal = report.count(FATAL) or any(code in ("1", "3", "4")
                                       for code, _ in report.set_errors)
    report.accepted = not fatal and (report.clean or not strict)
    if report.segments and not any(code == "5" for code, _ in report.set_errors):
        report.set_errors.append(("5", "one or more segments in error"))
    return report


# ---------------------------------------------------------------------------
# The envelope - what a truncated file or a renumbering batch tool gets wrong
# ---------------------------------------------------------------------------

# TA105 for an ISA element of the wrong width, by position. ISA16 is left out:
# it *is* the component separator, and one character by construction.
_ISA_NOTE = {1: "010", 2: "011", 3: "012", 4: "013", 5: "005", 6: "006",
             7: "007", 8: "008", 9: "014", 10: "015", 11: "016", 12: "017",
             13: "018", 14: "019", 15: "020"}


def _check_envelope(interchange: Interchange, report: InterchangeReport) -> None:
    """Trailers against headers, and counts against what actually arrived.

    Only a parsed interchange carries its header; one built in memory to be
    written has nothing to check, because the writer derives its trailers.
    """
    if interchange.header is None:
        return
    if interchange.dialect == "X12":
        _check_x12_envelope(interchange, report)
    else:
        _check_edifact_envelope(interchange, report)


def _same_number(left: str, right: str) -> bool:
    """Control numbers compared as numbers when both are: 000000077 is 77."""
    left, right = (left or "").strip(), (right or "").strip()
    if left.isdigit() and right.isdigit():
        return int(left) == int(right)
    return left == right


def _check_x12_envelope(interchange: Interchange, report: InterchangeReport) -> None:
    header = interchange.header
    findings = report.interchange_findings

    # Fixed widths. The parser splits on the element separator so that a
    # sloppy ISA can still be read; this is where it is held to the standard.
    # The receiver may well cope, so the mock notes it rather than refusing.
    for position, code in _ISA_NOTE.items():
        element = schema.ISA.element(position)
        value = header.get(position)
        if element is not None and len(value) != element.max_len:
            findings.append(EnvelopeFinding(
                code=code, tag="ISA", position=position, severity=ERROR,
                note="ISA%02d is %d characters, it is fixed at %d"
                     % (position, len(value), element.max_len)))

    explicit = [g for g in interchange.groups if not g.implicit]
    for group in explicit:
        errors: List[Tuple[str, str]] = []
        trailer = group.trailer
        if trailer is None:
            errors.append(("3", "group %s has no GE trailer" % group.control))
        else:
            if not _same_number(trailer.get(1), str(len(group.messages))):
                errors.append(("5", "GE01 counts %s transaction sets, the group "
                                    "holds %d" % (trailer.get(1) or "(empty)",
                                                  len(group.messages))))
            if not _same_number(trailer.get(2), group.control):
                errors.append(("4", "GE02 says group control number %s, GS06 "
                                    "says %s" % (trailer.get(2) or "(empty)",
                                                 group.control)))
        if errors:
            report.group_errors[(group.functional_id, group.control)] = errors

    trailer = interchange.trailer
    if trailer is None:
        findings.append(EnvelopeFinding(
            code="023", tag="IEA",
            note="the interchange ends without an IEA: the file was cut short"))
        return
    if not _same_number(trailer.get(1), str(len(explicit))):
        findings.append(EnvelopeFinding(
            code="021", tag="IEA", position=1,
            note="IEA01 counts %s functional groups, the interchange holds %d"
                 % (trailer.get(1) or "(empty)", len(explicit))))
    if not _same_number(trailer.get(2), header.get(13)):
        findings.append(EnvelopeFinding(
            code="001", tag="IEA", position=2,
            note="IEA02 says interchange control number %s, ISA13 says %s"
                 % (trailer.get(2) or "(empty)", header.get(13))))


def _check_edifact_envelope(interchange: Interchange,
                            report: InterchangeReport) -> None:
    findings = report.interchange_findings
    explicit = [g for g in interchange.groups if not g.implicit]
    for group in explicit:
        trailer = group.trailer
        if trailer is None:
            findings.append(EnvelopeFinding(
                code="13", tag="UNE",
                note="group %s has no UNE trailer" % group.control))
            continue
        if not _same_number(trailer.get(1), str(len(group.messages))):
            findings.append(EnvelopeFinding(
                code="29", tag="UNE", position=1,
                note="UNE counts %s messages, the group holds %d"
                     % (trailer.get(1) or "(empty)", len(group.messages))))
        if not _same_number(trailer.get(2), group.control):
            findings.append(EnvelopeFinding(
                code="28", tag="UNE", position=2,
                note="UNE says group reference %s, UNG says %s"
                     % (trailer.get(2) or "(empty)", group.control)))

    trailer = interchange.trailer
    if trailer is None:
        findings.append(EnvelopeFinding(
            code="13", tag="UNZ",
            note="the interchange ends without a UNZ: the file was cut short"))
        return
    # UNZ 0036 counts groups when there are any, and messages when there
    # are not.
    counted, what = ((len(explicit), "groups") if explicit
                     else (interchange.message_count, "messages"))
    if not _same_number(trailer.get(1), str(counted)):
        findings.append(EnvelopeFinding(
            code="29", tag="UNZ", position=1,
            note="UNZ counts %s %s, the interchange holds %d"
                 % (trailer.get(1) or "(empty)", what, counted)))
    if (trailer.get(2) or "").strip() != (interchange.control or "").strip():
        findings.append(EnvelopeFinding(
            code="28", tag="UNZ", position=2,
            note="UNZ says interchange reference %s, UNB says %s"
                 % (trailer.get(2) or "(empty)", interchange.control)))


# ---------------------------------------------------------------------------
# Trailer arithmetic - the checks that catch a generator, not a sender
# ---------------------------------------------------------------------------

def _check_trailer(message: Message, dialect: str, report: MessageReport) -> None:
    trailer_tag = "SE" if dialect == "X12" else "UNT"
    trailer = message.find(trailer_tag)
    if trailer is None:
        report.set_errors.append(("2", "the %s trailer is missing" % trailer_tag))
        return
    declared_control = trailer.get(2)
    if declared_control != message.control:
        report.set_errors.append((
            "3", "%s02 says control number %s, the header says %s"
            % (trailer_tag, declared_control or "(empty)", message.control)))
    declared = trailer.get(1)
    actual = len(message.segments)
    if declared and declared.isdigit() and int(declared) != actual:
        report.set_errors.append((
            "4", "%s01 counts %s segments, the message holds %d"
            % (trailer_tag, declared, actual)))


def _check_version(message: Message, dialect: str,
                   definition: schema.TransactionSet, report: MessageReport) -> None:
    """Compare UNH S009 0052/0054 with the directory the set is declared in.

    The dictionary is one directory - D.96A for business messages, syntax 3
    for CONTRL - so a message that names another is being read against
    definitions it does not claim. That is an error, not fatal: most
    directories agree on most segments, and a real translator configured for
    one release will often read another. X12 is left out until the dictionary
    is version-aware, since every 005010 set is read against 004010 today.
    """
    if dialect != "EDIFACT" or not definition.version or not message.version:
        return
    declared = definition.version.split(":")
    actual = message.version.split(":")
    header = message.segments[0] if message.segments else None
    findings = []
    for index in (1, 2):
        want = declared[index - 1] if index - 1 < len(declared) else ""
        got = actual[index - 1] if index - 1 < len(actual) else ""
        if got and want and got != want:
            findings.append(ElementFinding(
                position=2, component=index + 1,
                ref="0052" if index == 1 else "0054", code="7", value=got,
                note="UNH02 names %s:%s, the dictionary defines %s:%s"
                     % (message.code, message.version, message.code,
                        definition.version)))
    if findings and header is not None:
        report.segments.append(SegmentFinding(
            tag=header.tag, position=header.position, code="8",
            note="%s names a directory this mock does not define" % header.tag,
            elements=findings))


# ---------------------------------------------------------------------------
# Walking the message against the definition
# ---------------------------------------------------------------------------

class _Context:
    """One open loop - or the top level - while walking a message."""

    def __init__(self, loop: Optional[schema.Loop], definition):
        self.loop = loop
        self.children = definition.children if loop is None else loop.children
        self.uses = {child.tag: child for child in self.children
                     if isinstance(child, schema.Use)}
        self.loops = {child.trigger: child for child in self.children
                      if isinstance(child, schema.Loop)}
        self.seen: Dict[str, int] = {}
        # How many times each loop *inside* this context has started. Counting
        # per context rather than per transaction set matters: the 850 has an
        # N1 loop at the top level and another inside every PO1 loop, and they
        # are different loops that happen to share a name.
        self.repeats: Dict[str, int] = {}

    @property
    def id(self) -> str:
        return self.loop.id if self.loop else ""


def _walk(message: Message, definition: schema.TransactionSet,
          report: MessageReport) -> None:
    """Walk the segments, opening and closing loops as their triggers arrive.

    The subtlety is that a loop's trigger segment is also a *use* inside that
    loop - N1 both starts the N1 loop and is its first segment - so meeting
    the trigger again means the previous repetition ended, not that the
    segment was used twice.  Resolving outward and checking for that case
    first is what keeps a second N1 from being reported as an over-use.
    """
    stack: List[_Context] = [_Context(None, definition)]
    known_tags = set(definition.known_tags())

    for item in message.segments:
        target = _resolve(stack, item.tag, report, message, definition)
        if target is None:
            report.segments.append(SegmentFinding(
                tag=item.tag, position=item.position, loop=stack[-1].id,
                code="1" if item.tag not in known_tags else "2",
                note=("%s is not a segment this transaction set defines" % item.tag
                      if item.tag not in known_tags
                      else "%s is defined by %s but not at this point"
                           % (item.tag, definition.code)),
                severity=ERROR))
            continue

        context, loop = target
        if loop is not None:
            context.repeats[loop.id] = context.repeats.get(loop.id, 0) + 1
            if context.repeats[loop.id] > loop.repeat:
                report.segments.append(SegmentFinding(
                    tag=item.tag, position=item.position, loop=loop.id, code="4",
                    note="the %s loop repeats more than %d times"
                         % (loop.id, loop.repeat), severity=ERROR))
            stack.append(_Context(loop, definition))
            context = stack[-1]

        use = context.uses.get(item.tag)
        if use is None:
            continue
        context.seen[item.tag] = context.seen.get(item.tag, 0) + 1
        if context.seen[item.tag] > use.max_use:
            report.segments.append(SegmentFinding(
                tag=item.tag, position=item.position, loop=context.id, code="5",
                note="%s may be used %d time(s) here, this is use %d"
                     % (item.tag, use.max_use, context.seen[item.tag]),
                severity=ERROR))
        _check_elements(item, use.segment, context.id, report)

    while stack:
        _finish(stack.pop(), report, message)


def _resolve(stack: List[_Context], tag: str, report: MessageReport,
             message: Message, definition: schema.TransactionSet):
    """Find where this tag belongs, closing every loop it has moved past.

    Returns `(context, loop)`: the context the segment belongs to, and the
    loop it starts, if it starts one.  Contexts the segment has left are
    finished on the way out, which is when a loop's missing mandatory segments
    are reported.
    """
    for depth in range(len(stack) - 1, -1, -1):
        context = stack[depth]

        # A loop's own trigger, met again: the repetition ended and the next
        # one begins, in the *parent* context.
        if context.loop is not None and context.loop.trigger == tag and depth > 0:
            for index in range(len(stack) - 1, depth - 1, -1):
                _finish(stack[index], report, message)
            del stack[depth:]
            return stack[depth - 1], context.loop

        if tag in context.loops:
            for index in range(len(stack) - 1, depth, -1):
                _finish(stack[index], report, message)
            del stack[depth + 1:]
            return context, context.loops[tag]

        if tag in context.uses:
            for index in range(len(stack) - 1, depth, -1):
                _finish(stack[index], report, message)
            del stack[depth + 1:]
            return context, None
    return None


def _finish(context: _Context, report: MessageReport, message: Message) -> None:
    """Report the mandatory segments a closing loop never received.

    AK302 wants a position, and a segment that never arrived has none.  The
    trailer's position is the honest answer: by the end of the transaction
    set, it had not been seen.
    """
    for child in context.children:
        if isinstance(child, schema.Use) and child.req == schema.MANDATORY:
            if not context.seen.get(child.tag):
                report.segments.append(SegmentFinding(
                    tag=child.tag, position=len(message.segments), loop=context.id,
                    code="3",
                    note="%s is mandatory %s and is missing"
                         % (child.tag,
                            "in the %s loop" % context.id if context.id
                            else "in this transaction set"),
                    severity=FATAL))


# ---------------------------------------------------------------------------
# Elements
# ---------------------------------------------------------------------------

def _check_elements(item: Seg, definition: schema.Segment, loop: str,
                    report: MessageReport) -> None:
    findings: List[ElementFinding] = []
    for position in range(1, max(len(definition.elements), item.width) + 1):
        element = definition.element(position)
        raw = item.raw(position)
        if element is None:
            present = any(raw) if isinstance(raw, list) else bool(raw)
            if present:
                findings.append(ElementFinding(
                    position=position, component=0, ref="", code="3", value=str(raw),
                    note="%s has no element at position %d" % (definition.tag, position),
                    severity=ERROR))
            continue
        if element.composite:
            components = raw if isinstance(raw, list) else ([raw] if raw else [])
            # A mandatory component is only mandatory when the composite it
            # belongs to is present. NAD's C058 is optional; 3124 inside it is
            # mandatory; an absent C058 breaks nothing. Reading that the other
            # way makes almost every real EDIFACT document invalid.
            if not any(components):
                if element.req == schema.MANDATORY:
                    findings.append(ElementFinding(
                        position=position, component=0, ref=element.ref, code="1",
                        value="", severity=FATAL,
                        note="%s (%s) is mandatory and empty"
                             % (definition.label(position), element.name)))
                continue
            for index, sub in enumerate(element.components, start=1):
                value = components[index - 1] if index <= len(components) else ""
                findings.extend(_check_value(value, sub, position, index, definition))
        else:
            value = raw[0] if isinstance(raw, list) and raw else (
                "" if isinstance(raw, list) else raw)
            findings.extend(_check_value(value, element, position, 0, definition))

    if findings:
        report.segments.append(SegmentFinding(
            tag=item.tag, position=item.position, loop=loop, code="8",
            note="%s has data element errors" % item.tag,
            severity=FATAL if any(f.severity == FATAL for f in findings) else ERROR,
            elements=findings))


def _check_value(value: str, element: schema.Element, position: int,
                 component: int, definition: schema.Segment) -> List[ElementFinding]:
    label = definition.label(position)
    if component:
        label += "/%s" % element.ref

    def finding(code: str, note: str, severity: str = ERROR) -> ElementFinding:
        return ElementFinding(position=position, component=component, ref=element.ref,
                              code=code, value=value, note=note, severity=severity)

    if not value:
        if element.req == schema.MANDATORY:
            return [finding("1", "%s (%s) is mandatory and empty"
                            % (label, element.name), FATAL)]
        return []

    out: List[ElementFinding] = []
    if len(value) < element.min_len:
        out.append(finding("4", "%s is %d characters, the minimum is %d"
                           % (label, len(value), element.min_len)))
    if len(value) > element.max_len:
        out.append(finding("5", "%s is %d characters, the maximum is %d"
                           % (label, len(value), element.max_len)))
    if element.codes and value not in element.codes:
        out.append(finding("7", "%s is not a code %s accepts (%s)"
                           % (value, label, _some(element.codes))))
    if element.type in ("N0", "N1", "N2") and not _DIGITS.match(value):
        out.append(finding("6", "%s must be numeric, got %r" % (label, value)))
    if element.type == "R" and not _DECIMAL.match(value):
        out.append(finding("6", "%s must be a number, got %r" % (label, value)))
    if element.type == "DT" and parse_date(value) is None:
        out.append(finding("8", "%s is not a valid date: %r" % (label, value)))
    if element.type == "TM" and not _is_time(value):
        out.append(finding("9", "%s is not a valid time: %r" % (label, value)))
    return out


def _is_time(value: str) -> bool:
    if not value.isdigit() or len(value) not in (4, 6, 8):
        return False
    hour, minute = int(value[0:2]), int(value[2:4])
    second = int(value[4:6]) if len(value) >= 6 else 0
    return hour <= 23 and minute <= 59 and second <= 59


def _some(codes: Dict[str, str], limit: int = 6) -> str:
    keys = sorted(codes)
    shown = ", ".join(keys[:limit])
    return shown + ", ..." if len(keys) > limit else shown
