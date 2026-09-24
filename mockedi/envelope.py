"""The wire-level document model, shared by both dialects.

An EDI interchange is the same shape whichever standard wrote it: an envelope
holding groups, holding messages, holding segments, holding elements.  This
module is that shape, plus the delimiter handling that decides how it is
punctuated, and the sniffing that decides which dialect a lump of bytes is.

`x12.py` and `edifact.py` do the reading and writing.  Nothing here knows what
a purchase order is.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, Union

Value = Union[str, List[str]]


class EdiSyntaxError(ValueError):
    """The bytes are not a document of the dialect they claim to be.

    This is for damage that stops parsing - a missing ISA, an interchange
    trailer that never arrives.  Everything a document can get *wrong* while
    still parsing is a validation finding instead, and comes back in a 997 or
    a CONTRL rather than as an exception.
    """

    def __init__(self, message: str, position: int = 0, segment: str = ""):
        super().__init__(message)
        self.message = message
        self.position = position
        self.segment = segment


@dataclass(frozen=True)
class Delimiters:
    """How a document is punctuated.

    Both dialects declare their own delimiters in the document: X12 positionally
    inside the fixed-width ISA, EDIFACT in the optional UNA service string
    advice.  A reader that assumes the defaults works most of the time and then
    fails on the one partner who uses something else, so the mock always reads
    them from the document it was given and writes back the ones it was
    configured with.
    """
    segment: str = "~"
    element: str = "*"
    component: str = ">"
    repetition: str = "^"
    release: str = ""        # EDIFACT's escape character; X12 has none
    decimal: str = "."

    @property
    def all(self) -> Tuple[str, ...]:
        return tuple(c for c in (self.segment, self.element, self.component,
                                 self.repetition, self.release) if c)


X12_DEFAULTS = Delimiters()
EDIFACT_DEFAULTS = Delimiters(segment="'", element="+", component=":",
                              repetition="*", release="?", decimal=".")


@dataclass
class Seg:
    """One parsed segment.

    `elements` is 0-based internally and 1-based everywhere a human looks at
    it, because that is how the standards number positions: `seg.get(3)` is
    BEG03.  A composite element is a list of its components.
    """
    tag: str
    elements: List[Value] = field(default_factory=list)
    position: int = 0        # within its message; the header segment is 1

    def get(self, position: int, default: str = "") -> str:
        """Element at a 1-based position; the first component if it is composite."""
        value = self.raw(position)
        if isinstance(value, list):
            return value[0] if value else default
        return value if value != "" else default

    def raw(self, position: int) -> Value:
        if 1 <= position <= len(self.elements):
            return self.elements[position - 1]
        return ""

    def comp(self, position: int, component: int, default: str = "") -> str:
        """Component of a composite element, both 1-based: `seg.comp(1, 2)` is C002/1131."""
        value = self.raw(position)
        if isinstance(value, list):
            if 1 <= component <= len(value):
                return value[component - 1] or default
            return default
        return value if component == 1 and value else default

    def has(self, position: int) -> bool:
        value = self.raw(position)
        return bool(value) if not isinstance(value, list) else any(value)

    @property
    def width(self) -> int:
        return len(self.elements)

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return "Seg(%s, %r)" % (self.tag, self.elements)


@dataclass
class Message:
    """One transaction set (X12) or message (EDIFACT), header and trailer included."""
    code: str
    control: str
    segments: List[Seg] = field(default_factory=list)
    version: str = ""

    def find(self, tag: str) -> Optional[Seg]:
        for seg in self.segments:
            if seg.tag == tag:
                return seg
        return None

    def find_all(self, tag: str) -> List[Seg]:
        return [seg for seg in self.segments if seg.tag == tag]

    @property
    def body(self) -> List[Seg]:
        """Everything between the header and the trailer."""
        return self.segments[1:-1] if len(self.segments) >= 2 else []


@dataclass
class Group:
    """An X12 functional group.

    EDIFACT's UNG/UNE is so rarely used that the mock does not write one; an
    EDIFACT interchange parses into a single group with an empty
    `functional_id`, so that everything above this layer can treat the two
    dialects alike.
    """
    functional_id: str = ""
    sender: str = ""
    receiver: str = ""
    control: str = ""
    version: str = ""
    date: str = ""
    time: str = ""
    messages: List[Message] = field(default_factory=list)

    @property
    def implicit(self) -> bool:
        return not self.functional_id


@dataclass
class Interchange:
    """A whole interchange, from ISA/UNB to IEA/UNZ."""
    dialect: str = "X12"
    sender: str = ""
    sender_qualifier: str = ""
    receiver: str = ""
    receiver_qualifier: str = ""
    control: str = ""
    date: str = ""
    time: str = ""
    version: str = ""
    test: bool = False
    ack_requested: bool = False
    delimiters: Delimiters = X12_DEFAULTS
    groups: List[Group] = field(default_factory=list)

    def messages(self) -> Iterator[Tuple[Group, Message]]:
        for group in self.groups:
            for message in group.messages:
                yield group, message

    @property
    def message_count(self) -> int:
        return sum(len(group.messages) for group in self.groups)

    def codes(self) -> List[str]:
        return [message.code for _, message in self.messages()]


def sniff(payload: str) -> str:
    """Which dialect a document is, by its first segment.

    Cheap and reliable: an X12 interchange starts with the literal `ISA`, an
    EDIFACT one with `UNA` or `UNB`.  Anything else is not something the mock
    can read, and saying so early gives a better error than a parser failing
    halfway through.
    """
    text = payload.lstrip("\r\n\t ")
    if text[:3] == "ISA":
        return "X12"
    if text[:3] in ("UNA", "UNB"):
        return "EDIFACT"
    raise EdiSyntaxError(
        "not an EDI interchange: expected it to start with ISA (X12) or "
        "UNA/UNB (EDIFACT), got %r" % text[:16])


def split_segments(body: str, delims: Delimiters) -> List[str]:
    """Split on the segment terminator, honouring EDIFACT's release character.

    Trailing whitespace between segments is dropped: plenty of senders write
    one segment per line for readability, and a parser that treats the newline
    as data rejects perfectly good documents.
    """
    out: List[str] = []
    current: List[str] = []
    escaped = False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if delims.release and char == delims.release:
            current.append(char)
            escaped = True
            continue
        if char == delims.segment:
            text = "".join(current).strip("\r\n\t ")
            if text:
                out.append(text)
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip("\r\n\t ")
    if tail:
        out.append(tail)
    return out


def split_elements(segment: str, delims: Delimiters) -> List[Value]:
    """Split a segment into elements, and composite elements into components."""
    fields = _split(segment, delims.element, delims.release)
    out: List[Value] = []
    for raw in fields[1:]:
        if delims.component and delims.component in _unescaped(raw, delims.release):
            out.append([_unescape(part, delims.release)
                        for part in _split(raw, delims.component, delims.release)])
        else:
            out.append(_unescape(raw, delims.release))
    return out


def _split(text: str, delimiter: str, release: str) -> List[str]:
    if not release:
        return text.split(delimiter)
    out, current, escaped = [], [], False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == release:
            current.append(char)
            escaped = True
        elif char == delimiter:
            out.append("".join(current))
            current = []
        else:
            current.append(char)
    out.append("".join(current))
    return out


def _unescaped(text: str, release: str) -> str:
    """The text with escaped characters blanked out, for delimiter detection."""
    if not release:
        return text
    out, escaped = [], False
    for char in text:
        if escaped:
            out.append("\x00")
            escaped = False
        elif char == release:
            out.append("\x00")
            escaped = True
        else:
            out.append(char)
    return "".join(out)


def _unescape(text: str, release: str) -> str:
    if not release or release not in text:
        return text
    out, escaped = [], False
    for char in text:
        if escaped:
            out.append(char)
            escaped = False
        elif char == release:
            escaped = True
        else:
            out.append(char)
    return "".join(out)


def escape(value: str, delims: Delimiters) -> str:
    """Protect the delimiters inside a value.

    EDIFACT has a release character for this.  X12 does not - the standard's
    answer is that data must not contain the delimiters - so the mock strips
    them instead of writing a document nobody can parse.  Silently changing
    data is the lesser evil only because the alternative is a corrupt
    interchange; both are bad, which is the actual reason X12 implementations
    pick delimiters no one types.
    """
    text = "" if value is None else str(value)
    if not delims.release:
        for char in delims.all:
            text = text.replace(char, " ")
        return text
    out = []
    for char in text:
        if char in delims.all:
            out.append(delims.release)
        out.append(char)
    return "".join(out)


def render_segment(seg: Seg, delims: Delimiters) -> str:
    """One segment, trailing empty elements trimmed as every real sender does."""
    parts: List[str] = []
    for value in seg.elements:
        if isinstance(value, list):
            components = [escape(v, delims) for v in value]
            while components and components[-1] == "":
                components.pop()
            parts.append(delims.component.join(components))
        else:
            parts.append(escape(value, delims))
    while parts and parts[-1] == "":
        parts.pop()
    return delims.element.join([seg.tag] + parts)


def seg(tag: str, *elements: Value) -> Seg:
    """Build a segment, dropping nothing: `seg("BEG", "00", "SA", po, "", date)`."""
    return Seg(tag=tag, elements=[list(e) if isinstance(e, (list, tuple)) else
                                  ("" if e is None else str(e)) for e in elements])


# -- date and time, in the shapes both dialects use

def ccyymmdd(moment) -> str:
    return moment.strftime("%Y%m%d")


def yymmdd(moment) -> str:
    """The six-digit date ISA09 and UNB S004 still use."""
    return moment.strftime("%y%m%d")


def hhmm(moment) -> str:
    return moment.strftime("%H%M")


def parse_date(value: str):
    """A date in any of the widths the standards allow, or None.

    Six digits are a two-digit year, and the standards' own rule applies: 00-69
    is this century, 70-99 the last.  It is the Y2K windowing that never went
    away, because ISA09 is still six characters wide.
    """
    import datetime
    text = (value or "").strip()
    try:
        if len(text) == 8:
            return datetime.date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
        if len(text) == 6:
            year = int(text[0:2])
            year += 2000 if year < 70 else 1900
            return datetime.date(year, int(text[2:4]), int(text[4:6]))
    except ValueError:
        return None
    return None
