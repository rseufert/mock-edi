"""Reading and writing UN/EDIFACT interchanges.

The same job as `x12.py` for the other standard, and the differences are worth
naming because they are where converters break:

* **Delimiters are declared, or defaulted.**  The optional UNA service string
  advice sets all six punctuation characters.  When it is absent the defaults
  apply, and the defaults are not X12's.
* **There is a release character.**  `?` escapes the next character, so unlike
  X12 a description may legally contain a `+`.  Parsing that correctly is the
  difference between "ACME+SONS" and a segment that has grown an element.
* **Elements are composite.**  `BGM+220+PO4711+9` has three elements; the
  first two are composites that happen to carry one component each.
* **The decimal mark is declared too.**  UNA3 may say `,`, and German and
  Scandinavian partners use it.  It applies to numeric (`R`) elements only -
  a comma in a description is a comma - so it is translated against the
  dictionary: to `.` on the way in, back to the declared mark on the way out,
  and everything between the envelope and the business sees `12.50`.
* **Functional groups are optional and rare.**  UNG/UNE exists; almost nobody
  sends it.  An interchange parses into one implicit group so that everything
  above this layer can treat both dialects alike.
"""
from __future__ import annotations

import datetime
from typing import List, Optional, Sequence

from . import schema
from .envelope import (Delimiters, EDIFACT_DEFAULTS, EdiSyntaxError, Group,
                       Interchange, Message, Seg, ccyymmdd, hhmm,
                       render_segment, seg, split_elements, split_segments,
                       yymmdd)

UNA_LENGTH = 9  # "UNA" plus the six characters it declares


def read_delimiters(payload: str) -> Delimiters:
    """The punctuation, from UNA when the sender bothered to send one.

    UNA is six characters in a fixed order: component, element, decimal
    notation, release, reserved, segment.  The reserved position is always a
    space today; it is skipped rather than trusted.
    """
    text = payload.lstrip("\r\n\t ")
    if text[:3] != "UNA":
        return EDIFACT_DEFAULTS
    if len(text) < UNA_LENGTH:
        raise EdiSyntaxError("the UNA service string advice is truncated")
    component, element, decimal, release, _reserved, segment = text[3:UNA_LENGTH]
    return Delimiters(segment=segment, element=element, component=component,
                      repetition=EDIFACT_DEFAULTS.repetition,
                      release=release, decimal=decimal)


def parse(payload: str, delimiters: Optional[Delimiters] = None) -> Interchange:
    """An EDIFACT interchange, as far as it can be read."""
    delims = delimiters or read_delimiters(payload)
    text = payload.lstrip("\r\n\t ")
    if text[:3] == "UNA":
        text = text[UNA_LENGTH:]

    raw = split_segments(text, delims)
    if not raw:
        raise EdiSyntaxError("the interchange holds no segments")

    segments = []
    for item in raw:
        tag = item.split(delims.element, 1)[0].strip()
        segments.append(Seg(tag=tag, elements=split_elements(item, delims)))

    head = segments[0]
    if head.tag != "UNB":
        raise EdiSyntaxError(
            "the first segment is %s; an EDIFACT interchange starts with UNB"
            % head.tag, segment=head.tag)

    interchange = Interchange(
        dialect="EDIFACT",
        sender=head.comp(2, 1), sender_qualifier=head.comp(2, 2),
        receiver=head.comp(3, 1), receiver_qualifier=head.comp(3, 2),
        date=head.comp(4, 1), time=head.comp(4, 2),
        control=head.get(5),
        version="%s:%s" % (head.comp(1, 1), head.comp(1, 2)),
        ack_requested=head.get(9) == "1",
        test=head.get(11) == "1",
        delimiters=delims,
    )
    group = Group(sender=interchange.sender, receiver=interchange.receiver)
    interchange.groups.append(group)

    message: Optional[Message] = None
    position = 0
    for item in segments[1:]:
        tag = item.tag
        if tag == "UNG":
            group = Group(functional_id=item.get(1), sender=item.comp(2, 1),
                          receiver=item.comp(3, 1), control=item.get(5))
            interchange.groups.append(group)
        elif tag == "UNE":
            continue
        elif tag == "UNH":
            position = 1
            item.position = position
            message = Message(
                code=item.comp(2, 1), control=item.get(1),
                segments=[item],
                version="%s:%s:%s" % (item.comp(2, 2), item.comp(2, 3), item.comp(2, 4)))
            group.messages.append(message)
        elif tag == "UNT":
            if message is not None:
                position += 1
                item.position = position
                message.segments.append(item)
                message = None
        elif tag == "UNZ":
            break
        elif message is not None:
            position += 1
            item.position = position
            message.segments.append(item)

    # Drop the implicit group if the sender used real ones and it stayed empty.
    interchange.groups = [g for g in interchange.groups
                          if g.messages or not interchange.message_count]
    if delims.decimal not in (".", ""):
        for group in interchange.groups:
            for item in group.messages:
                item.segments = _decimals(item, delims.decimal, ".")
    return interchange


def _decimals(message: Message, old: str, new: str) -> List[Seg]:
    """The message's segments with the decimal mark in `R` elements swapped.

    Only elements the dictionary declares numeric are touched, and only in
    sets it defines; an unknown segment is left exactly as it arrived for the
    validator to report. A `.` where UNA declared `,` is left alone: ISO 9735
    admits either mark in data, and so does the mock.
    """
    definition = schema.lookup("EDIFACT", message.code)
    if definition is None:
        return list(message.segments)
    out = []
    for item in message.segments:
        segment = definition.segment_for(item.tag)
        if segment is None:
            out.append(item)
            continue
        elements = []
        for position, value in enumerate(item.elements, start=1):
            element = segment.element(position)
            if element is None:
                elements.append(value)
            elif element.composite:
                # A composite with one component arrives as a plain string.
                subs = element.components
                parts = [_mark(part, subs[i] if i < len(subs) else None, old, new)
                         for i, part in enumerate(
                             value if isinstance(value, list) else [value])]
                elements.append(parts if isinstance(value, list) else parts[0])
            else:
                elements.append(_mark(value, element, old, new))
        out.append(Seg(tag=item.tag, elements=elements, position=item.position))
    return out


def _mark(value, element: Optional[schema.Element], old: str, new: str):
    if isinstance(value, str) and element is not None and element.type == "R":
        return value.replace(old, new)
    return value


def message(code: str, control: str, body: Sequence[Seg],
            version: str = "D:96A:UN") -> Message:
    """Wrap body segments in UNH/UNT.

    UNT01 counts every segment of the message, UNH and UNT included - the same
    rule as SE01, and the same off-by-one waiting to happen.
    """
    parts = version.split(":")
    while len(parts) < 3:
        parts.append("")
    segments = [seg("UNH", control, [code] + parts[:3])]
    segments.extend(body)
    segments.append(seg("UNT", str(len(body) + 2), control))
    for index, item in enumerate(segments, start=1):
        item.position = index
    return Message(code=code, control=control, segments=segments, version=version)


def wrap(messages: Sequence[Message], sender: str, receiver: str, control: str,
         sender_qualifier: str = "ZZ", receiver_qualifier: str = "ZZ",
         syntax: str = "UNOC", syntax_version: str = "3",
         moment: Optional[datetime.datetime] = None, test: bool = False,
         ack_requested: bool = False,
         application_reference: str = "",
         delimiters: Optional[Delimiters] = None) -> Interchange:
    """Put messages into one interchange, with no functional group."""
    when = moment or datetime.datetime.now()
    group = Group(sender=sender, receiver=receiver, messages=list(messages))
    return Interchange(
        dialect="EDIFACT", sender=sender, sender_qualifier=sender_qualifier,
        receiver=receiver, receiver_qualifier=receiver_qualifier,
        control=control, date=yymmdd(when), time=hhmm(when),
        version="%s:%s" % (syntax, syntax_version),
        test=test, ack_requested=ack_requested,
        delimiters=delimiters or EDIFACT_DEFAULTS, groups=[group],
    )


def render(interchange: Interchange, newline: bool = False,
           una: bool = True, application_reference: str = "") -> str:
    """The interchange as it goes on the wire, UNA first unless asked otherwise."""
    delims = interchange.delimiters
    syntax = (interchange.version or "UNOC:3").split(":")
    while len(syntax) < 2:
        syntax.append("3")

    out: List[str] = []
    out.append(render_segment(seg(
        "UNB",
        [syntax[0], syntax[1]],
        [interchange.sender, interchange.sender_qualifier],
        [interchange.receiver, interchange.receiver_qualifier],
        [interchange.date, interchange.time],
        interchange.control,
        "",
        application_reference,
        "",
        "1" if interchange.ack_requested else "",
        "",
        "1" if interchange.test else "",
    ), delims))
    for group in interchange.groups:
        for item in group.messages:
            segments = (item.segments if delims.decimal in (".", "")
                        else _decimals(item, ".", delims.decimal))
            for element in segments:
                out.append(render_segment(element, delims))
    out.append(render_segment(
        seg("UNZ", str(interchange.message_count), interchange.control), delims))

    joiner = delims.segment + ("\n" if newline else "")
    body = joiner.join(out) + delims.segment + ("\n" if newline else "")
    if una:
        advice = "UNA%s%s%s%s %s" % (delims.component, delims.element,
                                     delims.decimal, delims.release, delims.segment)
        return advice + ("\n" if newline else "") + body
    return body
