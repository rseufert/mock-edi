"""Checking a document without trading on it, and the dictionary it is checked against.

`validate` is a POST, and refuses another method as it always did. The
dictionary is three routes: all of it, one dialect, one transaction set.
"""
from __future__ import annotations

from typing import Any, List, Tuple

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
                 "segments": list(item.known_tags())}
                for item in schema.SETS.values()],
        }
    dialect = rest[0].upper()
    if len(rest) == 1:
        return {"dialect": dialect,
                "transactionSets": sorted(code for d, code in schema.SETS
                                          if d == dialect)}
    if version and not schema.supports(dialect, version):
        return {"error": "no %s dictionary at version %s; this mock speaks %s"
                         % (dialect, version,
                            " and ".join(schema.VERSIONS.get(dialect, ())))}
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
        "segments": [
            {"tag": use.tag, "name": use.segment.name, "requirement": use.req,
             "maxUse": use.max_use, "loop": loop.id if loop else "",
             "purpose": use.segment.purpose,
             # `width` is how wide the standard makes the segment; the
             # elements below are the ones this mock checks. Where they differ,
             # the positions in between are carried and not validated - so a
             # guide that uses one of them is not wrong, it is untested.
             "width": use.segment.width,
             "checkedTo": len(use.segment.elements),
             "elements": [
                 {"position": position, "ref": element.ref, "name": element.name,
                  "type": element.type, "requirement": element.req,
                  "length": "%d/%d" % (element.min_len, element.max_len),
                  "codes": sorted(element.codes) if element.codes else None,
                  "components": [
                      {"ref": c.ref, "name": c.name, "requirement": c.req,
                       "codes": sorted(c.codes) if c.codes else None}
                      for c in element.components] or None}
                 for position, element in enumerate(use.segment.elements, start=1)]}
            for use, loop in definition.uses()],
    }
