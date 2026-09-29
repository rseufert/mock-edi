"""The route table, and the modules that fill it.

Each module here is one surface of the mock - the doors an interchange comes
through, the index page, the control plane - and registers its endpoints with
:func:`route`: a method, a path pattern and the function that answers it. The
handler looks a request up with :func:`find` and knows nothing about any
endpoint, so adding one touches one file here. It is mock-bank's table
(rseufert/mock-bank#44), so that a reader of one repo can read the other.

Where it differs, it differs to answer exactly as the mock always has (#182):

* A route can take ``ANY`` method. Most of the control plane never looked at
  the method - ``POST /_mock/catalog`` is the catalog - and the move changes
  no answer.
* A route can take the ``rest`` of the path as a list, which is how the
  control plane has always read ``/_mock/orders/<po>/timeline``.
* A ``/_mock`` pattern is matched as the control plane always matched it: the
  path starts with ``/_mock``, and the segments after the first are compared.
  Every other pattern is matched on the path exactly as sent, or one of its
  ``aliases``, so ``/edi/`` is the door only because it is listed as one.
* A route that names its method and is asked with another answers 405 with
  its own ``refuse`` line, which is what each door has always said.

Making the control plane as strict as mock-bank's is a change in behaviour,
and is #188's to decide.
"""
from __future__ import annotations

import json
import math
import urllib.parse
from typing import (Any, Callable, Dict, List, NamedTuple, Optional, Sequence,
                    Tuple)

# The method a route takes when it answers whatever it is asked with.
ANY = "*"
CONTROL = "/_mock"


class Route(NamedTuple):
    method: str
    pattern: str
    parts: Tuple[str, ...]
    aliases: Tuple[str, ...]
    rest: bool
    refuse: str
    function: Callable[..., Tuple[int, int]]


# Every endpoint, in the order the modules registered them. No two patterns
# match one path, so the order never decides which function answers.
TABLE: List[Route] = []


def route(method: str, pattern: str, aliases: Sequence[str] = (),
          rest: bool = False, refuse: str = ""):
    """Register the decorated function as the answer to ``method pattern``.

    The function is called with the handler, then the list of segments after
    the pattern's own when ``rest`` is set.
    """
    def register(function):
        TABLE.append(Route(method, pattern, tuple(segments(pattern)),
                           tuple(aliases), rest, refuse, function))
        return function
    return register


def find(method: str, path: str) -> Tuple[Optional[Route], List[Any]]:
    """The route for a request and its arguments, or ``(None, [])``.

    A route found at the path but for another method is returned all the
    same; the handler answers it with the route's ``refuse`` line.
    """
    elsewhere: Tuple[Optional[Route], List[Any]] = (None, [])
    for entry in TABLE:
        arguments = _match(entry, path)
        if arguments is None:
            continue
        if entry.method in (ANY, method):
            return entry, arguments
        if elsewhere[0] is None:
            elsewhere = (entry, arguments)
    return elsewhere


def _match(entry: Route, path: str) -> Optional[List[Any]]:
    if not entry.pattern.startswith(CONTROL):
        return [] if path in (entry.pattern,) + entry.aliases else None
    if not path.startswith(CONTROL):
        return None
    wanted, given = entry.parts[1:], segments(path)[1:]
    if given[:len(wanted)] != list(wanted):
        return None
    if entry.rest:
        return [given[len(wanted):]]
    return [] if len(given) == len(wanted) else None


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
from . import transport, control, index  # noqa: E402,F401
