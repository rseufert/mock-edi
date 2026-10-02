"""Checking a document without trading on it, and the dictionary it is checked against.

`validate` is a POST, and refuses another method as it always did. The
dictionary is three routes: all of it, one dialect, one transaction set.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import ack, charsets, edifact, partners, profiles, schema, validate, x12
from ..envelope import EdiSyntaxError, sniff
from . import first, route
from .transport import findings


@route("POST", "/_mock/validate", refuse="POST an interchange to validate it")
def validate_only(h) -> Tuple[int, int]:
    """Check an interchange and say what is wrong, changing nothing.

    The mock's validator, without the trading partner attached: useful
    while writing a mapping, when what you want is the findings and not
    four documents in a mailbox.
    """
    partner_id = first(h.query, "partner")
    http_charset = charsets.from_content_type(h.headers.get("Content-Type", ""))
    view = h.body.decode(charsets.BYTES)
    try:
        dialect = sniff(view)
        parts = x12.split(view) if dialect == "X12" else edifact.split(view)
        texts = []
        for part in parts:
            raw = part.encode(charsets.BYTES)
            texts.append(charsets.decode(
                raw, charsets.declared(dialect, raw, http_charset)))
        interchanges = [x12.parse(text) if dialect == "X12"
                        else edifact.parse(text) for text in texts]
    except EdiSyntaxError as error:
        return h.json(422, {"parsed": False, "error": str(error)})
    # `?partner=ACME` applies that partner's guide as well, which is the
    # check worth running before sending it anything.
    profile = profiles.load(h.mock.conn, partner_id) if partner_id else None
    reports = [validate.validate(item, profile=profile) for item in interchanges]
    interchange, report = interchanges[0], reports[0]
    return h.json(200, {
        "parsed": True,
        "dialect": dialect,
        "sender": interchange.sender,
        "receiver": interchange.receiver,
        "control": interchange.control,
        "clean": all(item.clean for item in reports),
        "groupCode": report.group_code,
        # As with the plain endpoint: the keys beside this one describe the
        # first interchange, and this describes each of them.
        "interchanges": [
            {"control": item.control, "sender": item.sender,
             "receiver": item.receiver, "clean": each.clean,
             "explain": ack.explain(each)}
            for item, each in zip(interchanges, reports)],
        "messages": [
            {"code": m.code, "control": m.control, "kind": m.kind,
             "accepted": m.accepted, "findings": findings(m)}
            for m in report.messages],
        "explain": ack.explain(report),
    })


@route("GET", "/_mock/dictionary")
@route("GET", "/_mock/dictionary/<dialect>")
@route("GET", "/_mock/dictionary/<dialect>/<code>")
def dictionary(h, *rest: str) -> Tuple[int, int]:
    profile = None
    partner_id = first(h.query, "partner")
    if partner_id:
        if partners.get(h.mock.conn, partner_id) is None:
            return h.json(404, {"error": "no partner %r" % partner_id})
        profile = profiles.load(h.mock.conn, partner_id)
    return h.json(200, _dictionary(list(rest), first(h.query, "version"), profile))


def _dictionary(rest: List[str], version: str = "", profile=None) -> Any:
    """The dictionary, served as data.

    Everything the mock validates against is derived from `schema.py`, so
    publishing it is not documentation that can go stale - it is the rules
    themselves.  A mapping tool can read this instead of a PDF.

    `?version=005010` serves a set as that version has it; without it, the
    set as declared. `?partner=ACME` serves it as that partner's guide
    narrows it, and names the guide.
    """
    if not rest:
        return {
            "dialects": list(schema.DIALECTS),
            "versions": {dialect: list(versions)
                         for dialect, versions in schema.VERSIONS.items()},
            "transactionSets": [
                {"dialect": item.dialect, "code": item.code, "name": item.name,
                 "kind": schema.kind_of(item.dialect, item.code),
                 "group": item.group, "version": item.version,
                 "purpose": item.purpose,
                 "segments": list(item.known_tags()),
                 "envelope": _envelope_path(item.dialect)}
                for item in schema.SETS.values()],
            # What goes round every set of a dialect, and belongs to none.
            "envelopes": [
                {"dialect": dialect, "path": _envelope_path(dialect),
                 "segments": [use.tag for use in uses]}
                for dialect, uses in schema.ENVELOPES.items()],
        }
    dialect = rest[0].upper()
    if len(rest) == 1:
        return {"dialect": dialect,
                "transactionSets": sorted(code for d, code in schema.SETS
                                          if d == dialect),
                "envelope": _envelope_path(dialect)}
    if version and not schema.supports(dialect, version):
        return {"error": "no %s dictionary at version %s; this mock speaks %s"
                         % (dialect, version,
                            " and ".join(schema.VERSIONS.get(dialect, ())))}
    if rest[1].lower() == ENVELOPE:
        return _envelope(dialect, version)
    definition = schema.lookup(dialect, rest[1].upper(), version)
    if definition is None:
        return {"error": "no transaction set %s/%s" % (dialect, rest[1])}
    narrowed = profile.narrow(definition) if profile is not None else None
    guide = profile.label if narrowed is not None else None
    definition = narrowed or definition
    return {
        "dialect": definition.dialect, "code": definition.code,
        "name": definition.name, "purpose": definition.purpose,
        "group": definition.group, "version": definition.version,
        "profile": guide,
        "envelope": _envelope_path(definition.dialect),
        "segments": [
            _segment(use.segment, use.req, use.max_use, loop.id if loop else "")
            for use, loop in definition.uses()],
    }


ENVELOPE = "envelope"


def _envelope_path(dialect: str) -> str:
    return "/_mock/dictionary/%s/%s" % (dialect, ENVELOPE)


def _envelope(dialect: str, version: str = "") -> Dict[str, Any]:
    """What goes round the transaction sets: ISA to IEA, or UNA to UNZ.

    One entry for each dialect, which every set shares - the envelope is part
    of no transaction set (#210). Each segment is described exactly as a
    set's segments are, with where it sits added: `level` is what it wraps
    and `role` which end of that it is, in the order they are on the wire.
    """
    uses = schema.ENVELOPES.get(dialect)
    if uses is None:
        return {"error": "no dialect %s" % dialect}
    segments = []
    for use in uses:
        entry = _segment(use.segment, use.req, use.max_use, "")
        entry["level"], entry["role"] = use.level, use.role
        # Not split on delimiters, when set: it is the segment that says
        # what they are.
        entry["fixedLength"] = use.fixed_length or None
        segments.append(entry)
    return {"dialect": dialect, "code": ENVELOPE,
            "name": "Interchange envelope",
            "purpose": "What goes round the %s: the interchange, and the "
                       "functional group inside it."
                       % ("transaction sets" if dialect == "X12" else "messages"),
            # The envelope's own: ISA12 for the X12 version asked for, and
            # the syntax version for EDIFACT, whatever directory its
            # messages are in. `setVersion` is the one `?version=` named.
            "version": _envelope_version(dialect, _set_version(dialect, version)),
            "setVersion": _set_version(dialect, version),
            "segments": segments}


def _set_version(dialect: str, version: str) -> str:
    """The version the dictionary keys its sets by, for the one asked for.

    A GS08 may carry an industry suffix - `005010X222` - which names a
    guide and not another standard, and a set asked for that way is served
    as 005010. The envelope says the same, and is not a 500 for it.
    """
    if not version:
        return schema.VERSIONS[dialect][0]
    return schema.base_version(dialect, version)


def _envelope_version(dialect: str, set_version: str) -> str:
    if dialect == "EDIFACT":
        return schema.EDIFACT_SYNTAX_VERSION
    return schema.ENVELOPE_VERSIONS[dialect][set_version]


def _repeats(element) -> Any:
    """Which header element this one has to say again, or None."""
    if not element.repeats:
        return None
    return {"tag": element.repeats[:-2], "position": int(element.repeats[-2:]),
            "label": element.repeats}


def _segment(segment, requirement: str, max_use: int, loop: str) -> Dict[str, Any]:
    return {
        "tag": segment.tag, "name": segment.name, "requirement": requirement,
        "maxUse": max_use, "loop": loop,
        "purpose": segment.purpose,
        # `width` is how wide the standard makes the segment; the
        # elements below are the ones this mock checks. Where they differ,
        # the positions in between are carried and not validated - so a
        # guide that uses one of them is not wrong, it is untested.
        "width": segment.width,
        "checkedTo": len(segment.elements),
        "elements": [
            {"position": position, "ref": element.ref, "name": element.name,
             "type": element.type, "requirement": element.req,
             "length": "%d/%d" % (element.min_len, element.max_len),
             "codes": sorted(element.codes) if element.codes else None,
             "components": [
                 {"ref": c.ref, "name": c.name, "requirement": c.req,
                  "codes": sorted(c.codes) if c.codes else None}
                 for c in element.components] or None,
             # IEA02 is ISA13 again: declared once in `schema.py`, where the
             # validator's check reads it too.
             "repeats": _repeats(element)}
            for position, element in enumerate(segment.elements, start=1)]}
