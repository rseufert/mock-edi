"""The wire-level document model, shared by both dialects.

An EDI interchange is the same shape whichever standard wrote it: an envelope
holding groups, holding messages, holding segments, holding elements.  This
module is that shape, plus the delimiter handling that decides how it is
punctuated, and the sniffing that decides which dialect a lump of bytes is.

`x12.py` and `edifact.py` do the reading and writing.  Nothing here knows what
a purchase order is.
"""
from __future__ import annotations

import datetime
import functools
import unicodedata
from dataclasses import dataclass, field
from typing import (Callable, Dict, Iterator, List, Optional, Sequence, Tuple,
                    Union)

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
    # Empty unless the document actually has one. 004010 does not - ISA11 is
    # the standards identifier there, always "U" - and EDIFACT syntax 3 does
    # not either, where UNA position 5 is reserved and written as a space. A
    # delimiter the document never declares still gets escaped or stripped out
    # of ordinary data, which is how `^` was disappearing from 004010 and how
    # an asterisk in a syntax 3 document came out as `?*`.
    repetition: str = ""
    release: str = ""        # EDIFACT's escape character; X12 has none
    decimal: str = "."

    @property
    def all(self) -> Tuple[str, ...]:
        return tuple(c for c in (self.segment, self.element, self.component,
                                 self.repetition, self.release) if c)


X12_DEFAULTS = Delimiters()
EDIFACT_DEFAULTS = Delimiters(segment="'", element="+", component=":",
                              release="?", decimal=".")


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
    # GS/GE or UNG/UNE as they arrived, for the envelope checks. A trailer of
    # None on a parsed group means it never came: the file ended first.
    header: Optional[Seg] = None
    trailer: Optional[Seg] = None

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
    # Segments that sit between ISA and the first GS, belonging to the
    # interchange rather than to any group. A TA1 is the one that matters: it
    # is a whole interchange's content, with no functional group anywhere.
    preamble: List[Seg] = field(default_factory=list)
    # ISA/IEA or UNB/UNZ as they arrived. Only a parsed interchange has them;
    # one built to be written leaves them empty and the writer derives both.
    header: Optional[Seg] = None
    trailer: Optional[Seg] = None

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


def cut_interchanges(payload: str, read_delimiters, trailer_tag: str) -> List[str]:
    """One string per interchange in a payload that may hold several.

    A file holding more than one interchange is ordinary on a VAN and over
    SFTP, and not rare over AS2.  Reading only the first is the kind of
    silence this mock exists not to produce.

    Each interchange is cut at *its own* trailer rather than at the first one
    found in the text, and its delimiters are read from its own header: two
    interchanges in one file need not be punctuated alike, and the second is
    entitled to declare its own.
    """
    parts: List[str] = []
    rest = payload
    while rest.strip("\r\n\t "):
        delims = read_delimiters(rest)   # raises if this is not an interchange
        end = _end_of_interchange(rest, delims, trailer_tag)
        parts.append(rest[:end])
        rest = rest[end:]
    return parts or [payload]


def _end_of_interchange(text: str, delims: Delimiters, trailer_tag: str) -> int:
    """Where the interchange's trailer ends, or the end of the text.

    Walked character by character rather than searched for, because EDIFACT's
    release character can escape a segment terminator, and because `IEA` and
    `UNZ` are ordinary text inside an element.
    """
    escaped = False
    start = 0
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if delims.release and char == delims.release:
            escaped = True
            continue
        if char != delims.segment:
            continue
        segment = text[start:index].strip("\r\n\t ")
        start = index + 1
        if segment.split(delims.element, 1)[0].strip() == trailer_tag:
            return start
    # No trailer at all: hand the whole of it over, and let the parser and the
    # envelope check report the truncation, which they already do.
    return len(text)


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


# a-z to A-Z, for a syntax whose repertoire has no lower case (#263). Built
# here rather than imported from `charsets` so that the syntax layer goes on
# depending on nothing above it.
_FOLD = str.maketrans("abcdefghijklmnopqrstuvwxyz",
                      "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


# What a letter becomes when the declared set cannot carry it (#264).
#
# There is no EDIFACT rule for this. The standard's only answer to a
# character outside the declared set is an error (0085 code 21), and what a
# sender does to avoid one is convention. This is the Unicode Consortium's:
# CLDR's `Latin-ASCII` transform, `common/transforms/Latin-ASCII.xml` at
# commit f68302c0ae, which is language-neutral. It removes the marks from
# Latin letters (so `ü` is `u`, never `ue`) and gives the letters that have
# no decomposition an explicit rule each. Those rules are copied here for
# the letters of Latin-1 Supplement and Latin Extended-A, which is where
# European names live, and for the capital sharp s.
#
# A German counterparty would write `Mueller` and a Danish one `Oere`. The
# mock writes `Muller` and `Ore`, on purpose: an envelope does not say what
# language a name is in, and `ue` is wrong for a Finnish or a Turkish `ü`.
# Zack's choice on #264, where the sources are.
_LETTERS = {
    "\u00c6": "AE", "\u00d0": "D", "\u00d8": "O", "\u00de": "TH",
    "\u00df": "ss", "\u00e6": "ae", "\u00f0": "d", "\u00f8": "o",
    "\u00fe": "th", "\u0110": "D", "\u0111": "d", "\u0126": "H",
    "\u0127": "h", "\u0131": "i", "\u0132": "IJ", "\u0133": "ij",
    "\u0138": "q", "\u013f": "L", "\u0140": "l", "\u0141": "L",
    "\u0142": "l", "\u0149": "'n", "\u014a": "N", "\u014b": "n",
    "\u0152": "OE", "\u0153": "oe", "\u0166": "T", "\u0167": "t",
    "\u017f": "s", "\u1e9e": "SS",
}


@functools.lru_cache(maxsize=4096)
def _carried(char: str, charset: str) -> bool:
    try:
        char.encode(charset)
    except UnicodeEncodeError:
        return False
    return True


@functools.lru_cache(maxsize=4096)
def _plain(char: str) -> str:
    """One character said in unaccented Latin letters, or itself if it has
    no such form: `\u0141` is `L`, `\u017a` is `z`, `\u00df` is `ss`."""
    if char in _LETTERS:
        return _LETTERS[char]
    base, marks = unicodedata.normalize("NFD", char)[:1], \
        unicodedata.normalize("NFD", char)[1:]
    # Marks come off Latin letters and digits only, as in CLDR: a Greek
    # letter without its accent is still a letter the set cannot carry.
    latin = base in _LETTERS or (base.isalnum() and (
        base.isascii() or unicodedata.name(base, "").startswith("LATIN")))
    if marks and latin and all(unicodedata.category(mark) == "Mn"
                               for mark in marks):
        return _LETTERS.get(base, base)
    return char


def transliterate(text: str, charset: str, grow: bool = True) -> str:
    """`text` with each character `charset` cannot carry said in plain letters.

    A character the set does carry is left exactly as it is: `\u00fc` stays
    `\u00fc` under ISO 8859-1 and becomes `u` only under ISO 646. One with no
    plain form is left for `fit` to substitute, as before.

    With `grow` off, a letter whose plain form is two characters is left
    alone too, for the caller that has no room for the second.
    """
    if text.isascii():
        return text
    # A letter and a mark sent as two characters are one letter.
    text = unicodedata.normalize("NFC", text)
    out: List[str] = []
    for char in text:
        if _carried(char, charset):
            out.append(char)
            continue
        if (unicodedata.category(char) == "Mn" and out
                and out[-1][-1:].isascii() and out[-1][-1:].isalnum()):
            continue            # a mark with no composed form: it comes off
        plain = _plain(char)
        out.append(plain if grow or len(plain) == 1 else char)
    return "".join(out)


def fit(value: str, charset: str, outside: str = "", fold: bool = False,
        limit: int = 0) -> str:
    """`value` with every character the set cannot carry said another way.

    A letter is transliterated - `\u0141\u00f3d\u017a` is `Lodz`, not `?\u00f3d?` -
    and whatever has no plain form is replaced by `?` (#264).

    `limit` is the element's maximum length, when the caller knows it. A
    few letters have a plain form of two characters (`\u00df` is `ss`), so a
    value that filled its element would outgrow it and the mock would write
    a document its own dictionary reports. So a value grows only into room
    it is known to have: with no `limit`, or with one the longer form would
    pass, those letters keep the `?` they had, which is one character. A
    full element loses a letter's legibility rather than its validity, and
    `fit` on its own never makes a value longer.

    `outside` is the repertoire's own exclusion, for a syntax whose codec
    admits more than the syntax does: `UNOA`'s codec is `ascii` and level A
    has no lower case, so the codec alone would pass `Widget` (#295). A
    character excluded here is substituted exactly as one the codec cannot
    encode is, in the same place and for the same reason.

    This belongs *before* `escape` and that is the whole point (#199). The
    substitution used to happen on the way to bytes, after the segment had
    been rendered, where nothing was left to protect the delimiters: in
    EDIFACT `?` is the release character, so a city of `Łódź` in a UNOC
    interchange went out as `?ód?+LD` and a conforming reader got eight
    elements where nine were written - the region swallowed, the postcode in
    its place and the country gone. Substituting first and escaping after
    turns that into a literal `?ód?`, which is lossy in the way any charset
    downgrade is and correct in its structure.

    An empty `charset` fits nothing, for the callers that do not know one.
    """
    text = "" if value is None else str(value)
    if charset and not text.isascii():
        # Only with a character set, and that is all that keeps X12 out of
        # this: `x12.render` calls `render_segment` too, and passes none.
        #
        # Before folding, so that `\u0142` is `l` and then `L` under level A.
        plain = transliterate(text, charset)
        if len(plain) > len(text) and not (limit and len(plain) <= limit):
            plain = transliterate(text, charset, grow=False)
        text = plain
    if fold:
        # Before the substitutions below: a folded character is then inside
        # the repertoire and the codec both, which is the point of folding.
        text = text.translate(_FOLD)
    if outside:
        text = "".join("?" if char in outside else char for char in text)
    if not charset:
        return text
    return text.encode(charset, "replace").decode(charset)


def render_segment(seg: Seg, delims: Delimiters, charset: str = "",
                   outside: str = "", fold: bool = False,
                   limit: Optional[Callable[[int, int], int]] = None) -> str:
    """One segment, trailing empty elements trimmed as every real sender does.

    `limit(element, component)` is the most characters that position holds,
    both counted from 1 and `component` 0 for an element that has none; 0
    where it is not known. See `fit` for what it is for.
    """
    def room(element: int, component: int = 0) -> int:
        return limit(element, component) if limit is not None else 0

    parts: List[str] = []
    for position, value in enumerate(seg.elements, start=1):
        if isinstance(value, list):
            components = [
                escape(fit(v, charset, outside, fold, room(position, index)),
                       delims)
                for index, v in enumerate(value, start=1)]
            while components and components[-1] == "":
                components.pop()
            parts.append(delims.component.join(components))
        else:
            parts.append(escape(fit(value, charset, outside, fold,
                                    room(position)), delims))
    while parts and parts[-1] == "":
        parts.pop()
    return delims.element.join([seg.tag] + parts)


def seg(tag: str, *elements: Value) -> Seg:
    """Build a segment, dropping nothing: `seg("BEG", "00", "SA", po, "", date)`."""
    return Seg(tag=tag, elements=[list(e) if isinstance(e, (list, tuple)) else
                                  ("" if e is None else str(e)) for e in elements])


# -- date and time, in the shapes both dialects use

class DocumentZone(datetime.tzinfo):
    """The zone a pinned mock dates its documents in: a fixed offset from UTC.

    A mock started at a chosen time (`--start-at`) has to write the same
    dates on any machine, so it cannot read them off the host's zone. It
    reads them in the zone its start time was written in instead, and its
    clock hands out moments carrying this, which `local` leaves as they are
    (#280). No daylight saving: an offset is an offset.
    """

    def __init__(self, offset: datetime.timedelta):
        self.offset = offset

    def utcoffset(self, moment):
        return self.offset

    def dst(self, moment):
        return datetime.timedelta(0)

    def tzname(self, moment):
        return datetime.timezone(self.offset).tzname(None)

    def __reduce__(self):
        # tzinfo's own would call __init__ with no offset, so a moment from a
        # pinned clock could not be copied or pickled.
        return (DocumentZone, (self.offset,))

    def __repr__(self) -> str:
        return "DocumentZone(%s)" % self.tzname(None)


def local(moment):
    """The same moment as the host's wall clock reads it.

    The mock keeps one clock, in UTC, because its control plane has to report
    timestamps a test in another zone can compare. The dates on the wire are
    the opposite: ISA09/10, GS04/05 and UNB S004 carry no zone and are the
    sender's local time by the standards' long convention, so they are
    written as the host reads them, whatever the clock underneath is in.

    A naive moment is already local and is left alone. So is one from a
    pinned mock's clock, which says its own zone: the host's has no say in a
    date that has to be the same on every machine.
    """
    zone = getattr(moment, "tzinfo", None)
    if zone is None or isinstance(zone, DocumentZone):
        return moment
    return moment.astimezone()


def ccyymmdd(moment) -> str:
    return local(moment).strftime("%Y%m%d")


def yymmdd(moment) -> str:
    """The six-digit date ISA09 and UNB S004 still use."""
    return local(moment).strftime("%y%m%d")


def hhmm(moment) -> str:
    return local(moment).strftime("%H%M")


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


# EDIFACT 2379, the format a DTM says its value is in, as (digits in the
# date, digits in the time after it). 2379 has hundreds of codes; these are
# the four the dictionary lists, and the ones a date arrives in.
EDIFACT_DATE_LAYOUTS = {"101": (6, 0), "102": (8, 0), "203": (8, 4), "204": (8, 6)}
EDIFACT_DATE_PICTURES = {"101": "YYMMDD", "102": "CCYYMMDD",
                         "203": "CCYYMMDDHHMM", "204": "CCYYMMDDHHMMSS"}


def parse_edifact_date(value: str, form: str = ""):
    """The date an EDIFACT DTM carries, read by the format it states, or None.

    `form` is 2379. A value is read only as what it says it is: eight digits
    called 203 are not a date and a time, and are not read as a date by the
    luck of their length (#209). With no format given, which the directory
    allows, the length decides. The time, where there is one, has to be a
    real time for the value to be read at all; only the date is returned.
    """
    text = (value or "").strip()
    if not text.isdigit():
        return None
    if form:
        layout = EDIFACT_DATE_LAYOUTS.get(form)
        if layout is None or len(text) != sum(layout):
            return None
    else:
        layout = {6: (6, 0), 8: (8, 0), 12: (8, 4), 14: (8, 6)}.get(len(text))
        if layout is None:
            return None
    clock = text[layout[0]:]
    if clock:
        hour, minute = int(clock[0:2]), int(clock[2:4])
        second = int(clock[4:6]) if len(clock) == 6 else 0
        if hour > 23 or minute > 59 or second > 59:
            return None
    return parse_date(text[:layout[0]])
