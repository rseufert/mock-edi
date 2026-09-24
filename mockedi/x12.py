"""Reading and writing ASC X12 interchanges.

The parser reads the delimiters out of the ISA rather than assuming them.
That is not defensiveness for its own sake: ISA is fixed width precisely so a
receiver can learn the punctuation from the first 106 characters, and a mock
that only accepted `*` and `~` would reject documents that are perfectly
legal - and would never catch the integration bug where a description
containing a `*` splits a segment in two.

Structure read and written:

    ISA ... interchange
      GS ... functional group
        ST ... transaction set
        SE
      GE
    IEA
"""
from __future__ import annotations

import datetime
from typing import List, Optional, Sequence

from .envelope import (Delimiters, EdiSyntaxError, Group, Interchange, Message,
                       Seg, Value, X12_DEFAULTS, ccyymmdd, hhmm,
                       render_segment, seg, split_elements, split_segments,
                       yymmdd)

ISA_ELEMENTS = 16


def read_delimiters(payload: str) -> Delimiters:
    """Learn the punctuation from the ISA, the way a real receiver does.

    The strict reading is positional - ISA16 is character 105 - which breaks on
    a sender who did not pad an element to its fixed width.  Splitting on the
    element separator instead gets the same answer from a well-formed ISA and
    still works on a sloppy one, and the fixed widths are checked during
    validation, where a complaint can be reported rather than fatal.
    """
    text = payload.lstrip("\r\n\t ")
    if text[:3] != "ISA":
        raise EdiSyntaxError("an X12 interchange starts with ISA, not %r" % text[:3])
    if len(text) < 20:
        raise EdiSyntaxError("the interchange is too short to hold an ISA segment")
    element = text[3]
    parts = text.split(element, ISA_ELEMENTS)
    if len(parts) <= ISA_ELEMENTS:
        raise EdiSyntaxError(
            "the ISA segment has %d elements; it must have exactly %d"
            % (len(parts) - 1, ISA_ELEMENTS))
    tail = parts[ISA_ELEMENTS]
    if len(tail) < 2:
        raise EdiSyntaxError("the ISA segment ends before ISA16 and its terminator")
    component, segment = tail[0], tail[1]
    # ISA11 carries the repetition separator from 00501 onward; in 00401 it is
    # the standards identifier, always "U", and repetition is not used.
    version = parts[12]
    repetition = parts[11] if version >= "00501" and parts[11] not in ("", "U") else "^"
    return Delimiters(segment=segment, element=element, component=component,
                      repetition=repetition, release="", decimal=".")


def parse(payload: str, delimiters: Optional[Delimiters] = None) -> Interchange:
    """An X12 interchange, as far as it can be read.

    Anything that stops the envelope being understood raises; anything a
    document can get wrong *inside* a readable envelope is left alone here and
    reported by `validate.py`, because that is what the 997 is for.
    """
    delims = delimiters or read_delimiters(payload)
    raw = split_segments(payload, delims)
    if not raw:
        raise EdiSyntaxError("the interchange holds no segments")

    segments = []
    for text in raw:
        fields = split_elements(text, delims)
        segments.append(Seg(tag=text.split(delims.element, 1)[0].strip(),
                            elements=fields))

    head = segments[0]
    if head.tag != "ISA":
        raise EdiSyntaxError("the first segment is %s; an interchange starts with ISA"
                             % head.tag, segment=head.tag)

    interchange = Interchange(
        dialect="X12",
        sender_qualifier=head.get(5), sender=head.get(6).strip(),
        receiver_qualifier=head.get(7), receiver=head.get(8).strip(),
        date=head.get(9), time=head.get(10),
        version=head.get(12), control=head.get(13).strip(),
        ack_requested=head.get(14) == "1",
        test=head.get(15) == "T",
        delimiters=delims,
    )

    group: Optional[Group] = None
    message: Optional[Message] = None
    position = 0
    for item in segments[1:]:
        tag = item.tag
        if tag == "GS":
            group = Group(functional_id=item.get(1), sender=item.get(2),
                          receiver=item.get(3), date=item.get(4), time=item.get(5),
                          control=item.get(6), version=item.get(8))
            interchange.groups.append(group)
        elif tag == "GE":
            group = None
        elif tag == "ST":
            if group is None:
                # A transaction set outside a group is malformed, but readable;
                # keep it in an implicit group so validation can say so.
                group = Group()
                interchange.groups.append(group)
            position = 1
            item.position = position
            message = Message(code=item.get(1), control=item.get(2),
                              segments=[item], version=group.version)
            group.messages.append(message)
        elif tag == "SE":
            if message is not None:
                position += 1
                item.position = position
                message.segments.append(item)
                message = None
        elif tag == "IEA":
            break
        elif message is not None:
            position += 1
            item.position = position
            message.segments.append(item)
    return interchange


def message(code: str, control: str, body: Sequence[Seg], version: str = "") -> Message:
    """Wrap body segments in ST/SE, with the count SE01 has to carry.

    SE01 counts ST and SE themselves; getting that off by one is the single
    most common reason a 997 comes back with AK5 error 4.
    """
    segments = [seg("ST", code, control)]
    segments.extend(body)
    segments.append(seg("SE", str(len(body) + 2), control))
    for index, item in enumerate(segments, start=1):
        item.position = index
    return Message(code=code, control=control, segments=segments, version=version)


def wrap(messages: Sequence[Message], sender: str, receiver: str,
         control: str, group_control: str, functional_id: str,
         sender_qualifier: str = "ZZ", receiver_qualifier: str = "ZZ",
         version: str = "004010", interchange_version: str = "00401",
         moment: Optional[datetime.datetime] = None, test: bool = False,
         ack_requested: bool = False,
         delimiters: Optional[Delimiters] = None) -> Interchange:
    """Put transaction sets into one functional group inside one interchange."""
    when = moment or datetime.datetime.now()
    group = Group(functional_id=functional_id, sender=sender, receiver=receiver,
                  control=group_control, version=version,
                  date=ccyymmdd(when), time=hhmm(when), messages=list(messages))
    return Interchange(
        dialect="X12", sender=sender, sender_qualifier=sender_qualifier,
        receiver=receiver, receiver_qualifier=receiver_qualifier,
        control=control, date=yymmdd(when), time=hhmm(when),
        version=interchange_version, test=test, ack_requested=ack_requested,
        delimiters=delimiters or X12_DEFAULTS, groups=[group])


def render(interchange: Interchange, newline: bool = False) -> str:
    """The interchange as it goes on the wire.

    `newline` puts each segment on its own line.  No receiver needs that - the
    terminator already ends the segment - but every human reading a captured
    document does, and a parser that cannot cope with it is broken anyway.
    """
    delims = interchange.delimiters
    out: List[str] = []
    out.append(_render_isa(interchange))
    groups = 0
    for group in interchange.groups:
        groups += 1
        out.append(render_segment(seg(
            "GS", group.functional_id, group.sender, group.receiver,
            group.date, group.time, group.control, "X", group.version), delims))
        for item in group.messages:
            for element in item.segments:
                out.append(render_segment(element, delims))
        out.append(render_segment(
            seg("GE", str(len(group.messages)), group.control), delims))
    out.append(render_segment(
        seg("IEA", str(groups), _pad_control(interchange.control)), delims))
    joiner = delims.segment + ("\n" if newline else "")
    return joiner.join(out) + delims.segment + ("\n" if newline else "")


def _render_isa(interchange: Interchange) -> str:
    """ISA, rendered without escaping - the one segment that must not be escaped.

    ISA16 *is* the component separator and ISA11 may be the repetition
    separator, so passing this segment through the usual escaping would strip
    the very characters it exists to declare.  Every element is padded to the
    fixed width the standard demands, which is what makes the segment exactly
    106 characters including its terminator.
    """
    header = _isa(interchange)
    return interchange.delimiters.element.join(
        [header.tag] + [str(value) for value in header.elements])


def _isa(interchange: Interchange) -> Seg:
    """ISA's sixteen fixed-width elements."""
    delims = interchange.delimiters
    # 00401 puts the standards identifier "U" in ISA11; 00501 puts the
    # repetition separator there.
    eleventh = "U" if interchange.version < "00501" else delims.repetition
    return seg(
        "ISA",
        "00", " " * 10, "00", " " * 10,
        _fixed(interchange.sender_qualifier, 2),
        _fixed(interchange.sender, 15),
        _fixed(interchange.receiver_qualifier, 2),
        _fixed(interchange.receiver, 15),
        interchange.date, interchange.time, eleventh, interchange.version,
        _pad_control(interchange.control),
        "1" if interchange.ack_requested else "0",
        "T" if interchange.test else "P",
        delims.component,
    )


def _fixed(value: str, width: int) -> str:
    text = (value or "")[:width]
    return text + " " * (width - len(text))


def _pad_control(value: str) -> str:
    """ISA13 and IEA02 are nine digits, zero filled."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit()) or "1"
    return digits[-9:].rjust(9, "0")
