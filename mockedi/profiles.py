"""A trading partner's implementation guide, as a narrowing of the dictionary.

The mock validates against the standard, and real integrations fail on the
partner's guide: the REF*IA the guide makes mandatory, the PO4 it does not use,
the unit codes it allows, the 15 characters it gives REF02. A profile says
those things, per partner and per set, and nothing else.

**A narrowing, never a widening.**  Everything a profile can say tightens what
the dictionary already allows - a segment required or forbidden, a use or a
loop repeated fewer times, a code list cut down, an element shortened or made
mandatory. A profile that would let through something the dictionary refuses
is refused when it is loaded, with the reason: a partner that accepts more than
004010 does is using another version, which is the dictionary's business.

**The same walk.**  A profile does not need a validator of its own. It derives
a tightened `TransactionSet` from the declared one, the way `schema.lookup`
derives 005010 from 004010, and the message is walked against that as well.
What the tightened walk finds that the plain one did not is the guide's
finding, and is marked with whose guide it is. Those findings are fatal: a
partner bounces a document its guide forbids.

The format is JSON, stored with the partner:

    {"name": "Acme 850 guide, v2",
     "sets": {"850": {
        "require": ["REF"],
        "forbid": ["PO1/PO4"],
        "segments": {"REF": {"maxUse": 2,
                             "elements": {"1": {"codes": ["IA"]},
                                          "2": {"maxLength": 15}}},
                     "PO1": {"elements": {"3": {"codes": ["EA", "CS"]}}}},
        "loops": {"PO1": {"repeat": 100}}}}}

**Where, not just what.**  A tag is named by where it is used, because a guide
that makes the header's `REF*IA` mandatory says nothing about the `REF*VN` on
each line. `REF` alone is the one at the top of the set; `PO1/REF` is the one
inside the PO1 loop; loops nest the same way, `PO1/N1`. A path that names a
loop - `N1`, `PO1/N1` - requires, forbids or limits the loop itself, and
under `segments` means the segment that starts it: `PO1` is `PO1/PO1`. An
element is named by its position, and a component of a composite by
`position.component` - `"2.1"`.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import db, schema

SET_KEYS = {"require", "forbid", "segments", "loops"}
SEGMENT_KEYS = {"maxUse", "elements"}
ELEMENT_KEYS = {"codes", "minLength", "maxLength", "required"}
LOOP_KEYS = {"repeat"}


class Invalid(ValueError):
    """A profile the mock cannot hold: malformed, or wider than the dictionary."""

    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Profile:
    partner: str
    name: str
    sets: Dict[str, Dict[str, Any]]

    @property
    def label(self) -> str:
        """How a finding names the rule's owner: whose guide, and which."""
        return "%s's guide %r" % (self.partner, self.name)

    def narrow(self, definition: schema.TransactionSet
               ) -> Optional[schema.TransactionSet]:
        """The set as this guide has it, or None if the guide says nothing
        about it."""
        spec = self.sets.get(definition.code)
        if spec is None:
            return None
        return dataclasses.replace(
            definition, children=_narrow_children(definition.children, spec))

    def as_json(self) -> Dict[str, Any]:
        return {"partner": self.partner, "name": self.name, "sets": self.sets}


# ---------------------------------------------------------------------------
# Loading: every problem named, and anything that widens refused
# ---------------------------------------------------------------------------

def check(partner: Dict[str, Any], body: Any) -> Profile:
    """A profile for `partner`, or `Invalid` naming everything wrong with it."""
    problems: List[str] = []
    if not isinstance(body, dict):
        raise Invalid(["a profile is a JSON object with a name and sets"])
    unknown = sorted(set(body) - {"name", "sets"})
    if unknown:
        problems.append("unknown field%s %s; a profile has name and sets"
                        % ("s" if len(unknown) > 1 else "", ", ".join(unknown)))
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("a profile needs a name, so a finding can say whose "
                        "rule it broke")
    sets = dict(body.get("sets") or {}) if isinstance(body.get("sets"), dict) else body.get("sets")
    if not isinstance(sets, dict) or not sets:
        problems.append("a profile needs sets: {set code: narrowings}")
        raise Invalid(problems)

    dialect = partner["dialect"]
    for code, spec in sets.items():
        definition = schema.lookup(dialect, code)
        if definition is None:
            problems.append("%s has no transaction set %r"
                            % (dialect, code))
            continue
        spec = _normalise(definition, spec)
        sets[code] = spec
        problems.extend(_check_set(definition, spec))
    if problems:
        raise Invalid(problems)
    return Profile(partner=partner["id"], name=name.strip(), sets=sets)


def _normalise(definition: schema.TransactionSet, spec: Any) -> Any:
    """Under `segments`, a loop's path stands for the segment that starts it."""
    if not isinstance(spec, dict) or not isinstance(spec.get("segments"), dict):
        return spec
    segments = {}
    for path, narrowing in spec["segments"].items():
        loop, _uses = _at(definition, path)
        segments[path + "/" + loop.trigger if loop is not None else path] = narrowing
    return dict(spec, segments=segments)


def _paths(children, prefix: str = ""):
    """Every use and loop in a set, with the path a profile names it by."""
    for child in children:
        if isinstance(child, schema.Loop):
            path = prefix + child.id
            yield path, child
            yield from _paths(child.children, path + "/")
        else:
            yield prefix + child.tag, child


def _at(definition: schema.TransactionSet, path: str):
    """The loop a path names, or the uses it names, or neither."""
    found = [node for where, node in _paths(definition.children) if where == path]
    loops = [node for node in found if isinstance(node, schema.Loop)]
    if loops:
        return loops[0], []
    return None, [node for node in found if isinstance(node, schema.Use)]


def _where(definition: schema.TransactionSet) -> str:
    """The paths a set has, for an error that names a wrong one."""
    return ", ".join(path for path, _node in _paths(definition.children))


def _check_set(definition: schema.TransactionSet, spec: Any) -> List[str]:
    where = definition.code
    if not isinstance(spec, dict):
        return ["%s: the narrowings for a set are a JSON object" % where]
    problems = ["%s: unknown field %s; a set may say %s"
                % (where, key, ", ".join(sorted(SET_KEYS)))
                for key in sorted(set(spec) - SET_KEYS)]

    required = spec.get("require", [])
    forbidden = spec.get("forbid", [])
    for key, paths in (("require", required), ("forbid", forbidden)):
        if not isinstance(paths, list) or not all(isinstance(t, str) for t in paths):
            problems.append("%s: %s is a list of segment or loop paths" % (where, key))
            continue
        for path in paths:
            loop, uses = _at(definition, path)
            if loop is None and not uses:
                problems.append("%s: %s is nowhere in %s, so the guide cannot %s "
                                "it; its paths are %s"
                                % (where, path, where, key, _where(definition)))
    for path in forbidden if isinstance(forbidden, list) else ():
        loop, uses = _at(definition, path)
        mandatory = (loop.req if loop is not None else None) == schema.MANDATORY \
            or any(use.req == schema.MANDATORY for use in uses)
        if mandatory:
            problems.append("%s: %s is mandatory in %s; forbidding it would "
                            "refuse every document the standard allows"
                            % (where, path, where))
        if path in (required if isinstance(required, list) else ()):
            problems.append("%s: %s is both required and forbidden" % (where, path))

    segments = spec.get("segments", {})
    if not isinstance(segments, dict):
        problems.append("%s: segments is {path: narrowings}" % where)
        segments = {}
    for path, narrowing in segments.items():
        _loop, uses = _at(definition, path)
        if not uses:
            problems.append("%s: %s is not a segment %s uses; its paths are %s"
                            % (where, path, where, _where(definition)))
            continue
        problems.extend(_check_segment(where, path, uses, narrowing))

    loops = spec.get("loops", {})
    if not isinstance(loops, dict):
        problems.append("%s: loops is {loop path: narrowings}" % where)
        loops = {}
    for loop_id, narrowing in loops.items():
        loop, _uses = _at(definition, loop_id)
        if loop is None:
            problems.append("%s: there is no %s loop" % (where, loop_id))
            continue
        if not isinstance(narrowing, dict):
            problems.append("%s: the %s loop's narrowings are a JSON object"
                            % (where, loop_id))
            continue
        problems.extend("%s: the %s loop: unknown field %s" % (where, loop_id, key)
                        for key in sorted(set(narrowing) - LOOP_KEYS))
        repeat = narrowing.get("repeat")
        if repeat is not None and not _count_within(repeat, loop.repeat):
            problems.append("%s: the %s loop repeats at most %d times; a guide "
                            "may lower that, to at least 1, not raise it (%r)"
                            % (where, loop_id, loop.repeat, repeat))
    return problems


def _count_within(value: Any, limit: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= limit


def _check_segment(where: str, tag: str, uses: List[schema.Use],
                   narrowing: Any) -> List[str]:
    if not isinstance(narrowing, dict):
        return ["%s: %s's narrowings are a JSON object" % (where, tag)]
    problems = ["%s: %s: unknown field %s; a segment may say %s"
                % (where, tag, key, ", ".join(sorted(SEGMENT_KEYS)))
                for key in sorted(set(narrowing) - SEGMENT_KEYS)]
    max_use = narrowing.get("maxUse")
    least = min(use.max_use for use in uses)
    if max_use is not None and not _count_within(max_use, least):
        problems.append("%s: %s may be used %d time(s); a guide may lower that, "
                        "to at least 1, not raise it (%r)"
                        % (where, tag, least, max_use))
    elements = narrowing.get("elements", {})
    if not isinstance(elements, dict):
        return problems + ["%s: %s: elements is {position: narrowings}" % (where, tag)]
    segment = uses[0].segment
    for position, rules in elements.items():
        element = _element_at(segment, position)
        label = "%s%s" % (tag, _label(position))
        if "/" in tag:
            label = "%s %s%s" % (tag, uses[0].tag, _label(position))
        if element is None:
            problems.append(
                "%s: %s is not an element the dictionary checks, so a guide "
                "cannot narrow it" % (where, label))
            continue
        problems.extend(_check_element(where, label, element, rules))
    return problems


def _label(position: str) -> str:
    whole, _, component = str(position).partition(".")
    try:
        text = "%02d" % int(whole)
    except ValueError:
        return str(position)
    return text + ("-%s" % component if component else "")


def _element_at(segment: schema.Segment, position: str) -> Optional[schema.Element]:
    whole, _, component = str(position).partition(".")
    try:
        element = segment.element(int(whole))
        if component:
            index = int(component)
            if element is None or not 1 <= index <= len(element.components):
                return None
            return element.components[index - 1]
    except ValueError:
        return None
    if element is not None and element.composite:
        return None      # narrow a component, not the composite as a whole
    return element


def _check_element(where: str, label: str, element: schema.Element,
                   rules: Any) -> List[str]:
    if not isinstance(rules, dict):
        return ["%s: %s's narrowings are a JSON object" % (where, label)]
    problems = ["%s: %s: unknown field %s; an element may say %s"
                % (where, label, key, ", ".join(sorted(ELEMENT_KEYS)))
                for key in sorted(set(rules) - ELEMENT_KEYS)]
    codes = rules.get("codes")
    if codes is not None:
        if not isinstance(codes, list) or not codes or not all(
                isinstance(c, str) and c for c in codes):
            problems.append("%s: %s: codes is a list of the codes the guide "
                            "allows" % (where, label))
        elif element.codes:
            wider = sorted(set(codes) - set(element.codes))
            if wider:
                problems.append("%s: %s: %s %s not in the dictionary's list for "
                                "it; a guide can only allow fewer"
                                % (where, label, ", ".join(wider),
                                   "is" if len(wider) == 1 else "are"))
    low, high = rules.get("minLength"), rules.get("maxLength")
    for key, value in (("minLength", low), ("maxLength", high)):
        if value is not None and not (isinstance(value, int)
                                      and not isinstance(value, bool)):
            problems.append("%s: %s: %s is a number" % (where, label, key))
    if isinstance(high, int) and not isinstance(high, bool) and not (
            element.min_len <= high <= element.max_len):
        problems.append("%s: %s is %d to %d characters; a guide may shorten its "
                        "maximum, not lengthen it (%d)"
                        % (where, label, element.min_len, element.max_len, high))
    if isinstance(low, int) and not isinstance(low, bool) and not (
            element.min_len <= low <= element.max_len):
        problems.append("%s: %s is %d to %d characters; a guide may raise its "
                        "minimum, not lower it (%d)"
                        % (where, label, element.min_len, element.max_len, low))
    if (isinstance(low, int) and isinstance(high, int) and low > high):
        problems.append("%s: %s: minLength %d is above maxLength %d"
                        % (where, label, low, high))
    required = rules.get("required")
    if required is not None:
        if not isinstance(required, bool):
            problems.append("%s: %s: required is true or false" % (where, label))
        elif not required and element.req == schema.MANDATORY:
            problems.append("%s: %s is mandatory in the dictionary; a guide "
                            "cannot make it optional" % (where, label))
    return problems


# ---------------------------------------------------------------------------
# Narrowing: the tightened set a message is also walked against
# ---------------------------------------------------------------------------

def _narrow_children(children, spec: Dict[str, Any], prefix: str = ""):
    required = set(spec.get("require", []))
    forbidden = set(spec.get("forbid", []))
    segments = spec.get("segments", {})
    loops = spec.get("loops", {})
    out = []
    for child in children:
        if isinstance(child, schema.Loop):
            path = prefix + child.id
            if path in forbidden:
                continue
            changes: Dict[str, Any] = {
                "children": _narrow_children(child.children, spec, path + "/")}
            if path in loops and "repeat" in loops[path]:
                changes["repeat"] = loops[path]["repeat"]
            if path in required:
                changes["req"] = schema.MANDATORY
            out.append(dataclasses.replace(child, **changes))
            continue
        path = prefix + child.tag
        if path in forbidden:
            continue
        changes = {}
        if path in required:
            changes["req"] = schema.MANDATORY
        narrowing = segments.get(path, {})
        if "maxUse" in narrowing:
            changes["max_use"] = min(child.max_use, narrowing["maxUse"])
        if narrowing.get("elements"):
            changes["segment"] = _narrow_segment(child.segment, narrowing["elements"])
        out.append(dataclasses.replace(child, **changes) if changes else child)
    return tuple(out)


def _narrow_segment(segment: schema.Segment, elements: Dict[str, Any]) -> schema.Segment:
    narrowed = list(segment.elements)
    for position, rules in elements.items():
        whole, _, component = str(position).partition(".")
        index = int(whole) - 1
        if component:
            composite = narrowed[index]
            parts = list(composite.components)
            parts[int(component) - 1] = _narrow_element(parts[int(component) - 1], rules)
            narrowed[index] = dataclasses.replace(composite, components=tuple(parts))
        else:
            narrowed[index] = _narrow_element(narrowed[index], rules)
    return dataclasses.replace(segment, elements=tuple(narrowed))


def _narrow_element(element: schema.Element, rules: Dict[str, Any]) -> schema.Element:
    changes: Dict[str, Any] = {}
    if "codes" in rules:
        known = element.codes or {}
        changes["codes"] = {code: known.get(code, "allowed by the guide")
                            for code in rules["codes"]}
    if "minLength" in rules:
        changes["min_len"] = rules["minLength"]
    if "maxLength" in rules:
        changes["max_len"] = rules["maxLength"]
    if rules.get("required"):
        changes["req"] = schema.MANDATORY
    return dataclasses.replace(element, **changes)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def load(conn: sqlite3.Connection, partner_id: str) -> Optional[Profile]:
    row = db.one(conn, "SELECT profile FROM partner_profile WHERE partner = ?",
                 (partner_id,))
    if row is None:
        return None
    data = json.loads(row["profile"])
    return Profile(partner=partner_id, name=data["name"], sets=data["sets"])


def save(conn: sqlite3.Connection, profile: Profile) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO partner_profile (partner, profile, at)"
        " VALUES (?,?,?)",
        (profile.partner, json.dumps({"name": profile.name, "sets": profile.sets}),
         db.now(conn)))
    conn.commit()


def remove(conn: sqlite3.Connection, partner_id: str) -> bool:
    removed = conn.execute("DELETE FROM partner_profile WHERE partner = ?",
                           (partner_id,)).rowcount
    conn.commit()
    return bool(removed)
