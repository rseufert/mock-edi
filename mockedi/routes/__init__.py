"""The route table, and the modules that fill it.

Each module here is one surface of the mock - the doors an interchange comes
through, the index page, the control plane - and registers its endpoints with
:func:`route`: a method, a path pattern and the function that answers it. The
handler looks a request up with :func:`find` and knows nothing about any
endpoint, so adding one touches one file here. It is mock-bank's table
(rseufert/mock-bank#44), so that a reader of one repo can read the other.

A pattern is written the way the 404 lists it, with ``<name>`` for a segment
that can be anything: ``/_mock/orders/<po>/timeline``. A route function is
called with the handler and each placeholder's segment, percent-decoded, in
order.

**A request is matched on its method and its path exactly** (#188). Until
then the control plane answered any method on a read endpoint, ignored
segments past the ones it knew, and took any path that began with ``/_mock``
- so ``/_mockery/health`` was the health check. Now:

* The path is the pattern, segment for segment. A trailing slash, an empty
  segment or an extra one is not it: 404.
* The method is the one registered. Another is a 405 that says what to send,
  in the route's ``refuse`` line, with an ``Allow`` header.
* A door keeps the other names it has always answered under, as ``aliases``:
  ``/as2/`` and ``/as2/receive`` are what a partner's AS2 software was
  configured with once and never looked at again. They are listed names, not
  loose matching.
"""
from __future__ import annotations

import json
import math
import urllib.parse
from typing import (Any, Callable, Dict, List, NamedTuple, Optional, Sequence,
                    Tuple)

CONTROL = "/_mock"


class Route(NamedTuple):
    method: str
    pattern: str
    parts: Tuple[str, ...]
    aliases: Tuple[str, ...]
    refuse: str
    function: Callable[..., Tuple[int, int]]


# Every endpoint, in the order the modules registered them. The order is the
# order a 405 lists the allowed methods in and the 404 lists the endpoints
# in; no two registrations match one path with the same method, so it never
# decides which function answers.
TABLE: List[Route] = []


def route(method: str, pattern: str, aliases: Sequence[str] = (),
          refuse: str = ""):
    """Register the decorated function as the answer to ``method pattern``.

    ``refuse`` is the line a 405 says when the path is asked with a method
    nothing is registered for; left out, the line names the methods that are.
    One function can be registered more than once, for each method or
    pattern it answers.
    """
    def register(function):
        TABLE.append(Route(method, pattern, tuple(segments(pattern)),
                           tuple(aliases), refuse, function))
        return function
    return register


def find(method: str, path: str) -> Tuple[Optional[Route], List[str], List[str]]:
    """The route for a request, its arguments, and the methods allowed.

    ``(route, arguments, [])`` when the path and the method match.
    ``(route, [], allowed)`` when the path matches only with other methods:
    a 405, and the route is one registered there - the one that carries a
    ``refuse`` line, if any does. ``(None, [], [])`` when nothing matches: a
    404.
    """
    elsewhere: Optional[Route] = None
    allowed: List[str] = []
    for entry in TABLE:
        arguments = _match(entry, path)
        if arguments is None:
            continue
        if entry.method == method:
            return entry, arguments, []
        if elsewhere is None or (entry.refuse and not elsewhere.refuse):
            elsewhere = entry
        if entry.method not in allowed:
            allowed.append(entry.method)
    return elsewhere, [], sorted(allowed)


def _match(entry: Route, path: str) -> Optional[List[str]]:
    if path in entry.aliases:
        return []
    if not entry.pattern.startswith(CONTROL):
        return [] if path == entry.pattern else None
    # Split as sent, before any segment is decoded: an empty piece is a
    # doubled or a trailing slash, and neither is the pattern.
    pieces = path.split("/")[1:]
    if len(pieces) != len(entry.parts) or "" in pieces:
        return None
    arguments: List[str] = []
    for wanted, piece in zip(entry.parts, pieces):
        given = urllib.parse.unquote(piece)
        if wanted.startswith("<") and wanted.endswith(">"):
            arguments.append(given)
        elif wanted != given:
            return None
    return arguments


def refusal(found: Route, allowed: Sequence[str]) -> str:
    """What a 405 says: the route's own line, or the methods it does take."""
    return found.refuse or "%s %s" % (" or ".join(allowed), found.pattern)


def endpoints() -> List[str]:
    """The control plane's endpoints by name, in the order registered."""
    names: List[str] = []
    for entry in TABLE:
        if len(entry.parts) > 1 and entry.parts[0] == CONTROL[1:] \
                and entry.parts[1] not in names:
            names.append(entry.parts[1])
    return names


def not_found(method: str, path: str) -> Dict[str, Any]:
    """The body of a 404: what was asked for, and what there is instead."""
    if path != CONTROL and not path.startswith(CONTROL + "/"):
        return {"error": "no route for %s %s" % (method, path),
                "try": ["/as2", "/edi", "/_mock/health", "/"]}
    given = segments(path)
    name = given[1] if len(given) > 1 else ""
    if name in endpoints():
        # The endpoint exists and this is not one of its paths: a trailing
        # slash, a segment too many, a name it does not have.
        return {"error": "no route for %s %s" % (method, path),
                "routes": ["%s %s" % (entry.method, entry.pattern)
                           for entry in TABLE
                           if len(entry.parts) > 1 and entry.parts[1] == name]}
    return {"error": "no control endpoint %r" % name, "endpoints": endpoints()}


# ---------------------------------------------------------------------------
# What a route reads from the request
# ---------------------------------------------------------------------------

def split(target: str) -> Tuple[str, Dict[str, List[str]]]:
    parsed = urllib.parse.urlsplit(target)
    # The path stays encoded: it is split on `/` before any segment is
    # decoded, or an encoded slash in a PO number (`PO%2F2026%2F1`) would
    # become a real one and the order could never be reached. `segments`
    # decodes each piece, once.
    # `keep_blank_values` matters: the flags are written `?all`, `?raw`,
    # `?leave`, with no value at all, and the default parse drops them - so
    # every flag silently read as false.
    return (parsed.path,
            urllib.parse.parse_qs(parsed.query, keep_blank_values=True))


def segments(path: str) -> List[str]:
    """The segments of a still-encoded path, each percent-decoded once."""
    return [urllib.parse.unquote(p) for p in path.split("/") if p]


def first(query: Dict[str, List[str]], name: str, default: str = "") -> str:
    values = query.get(name) or []
    return values[0] if values else default


def flag(query: Dict[str, List[str]], name: str) -> bool:
    value = first(query, name, "").lower()
    return value in ("", "1", "true", "yes") and name in query


class BadQuery(ValueError):
    """A query-string value that cannot be read; answered 400, naming it."""

    def __init__(self, parameter: str, message: str):
        super().__init__(message)
        self.parameter = parameter


def number(query: Dict[str, List[str]], name: str, default: float = 0.0) -> float:
    raw = first(query, name)
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value):
        raise BadQuery(name, "%s must be a number, got %r" % (name, raw))
    return value


def limit(query: Dict[str, List[str]], default: int = 50) -> int:
    raw = first(query, "limit")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadQuery("limit", "limit must be a whole number, got %r" % raw) from None
    return max(1, min(1000, value))


def only(query: Dict[str, List[str]], *names: str) -> None:
    """Refuse a query parameter the endpoint does not take.

    One that is ignored reads as one that worked: `advance?days=30` answered
    200 and moved nothing (#203). The first unknown name is the one reported,
    in the order a caller would look for it.
    """
    unknown = sorted(name for name in query if name not in names)
    if unknown:
        raise BadQuery(unknown[0], "%r is not a parameter here; this takes %s"
                       % (unknown[0], ", ".join(names)))


def json_body(body: bytes) -> Dict[str, Any]:
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# Imported last, so that `route` and the helpers above exist when each module
# asks for them. The order here is the order of the table.
from . import (transport, control, validate, orders, documents,  # noqa: E402,F401
               partners, mailbox, clock, index)
