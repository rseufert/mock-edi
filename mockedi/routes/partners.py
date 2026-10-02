"""Who the mock trades with, each one's implementation guide, and what it sells.

Registered for `ANY` method with the `rest` of the path read as the control
plane's old `if` chain read it (#182). A partner and its profile check the method themselves, and
refuse the others in the words they always used.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .. import db, delivery, partners, profiles
from . import ANY, json_body, route


@route(ANY, "/_mock/partners", rest=True)
def partner(h, rest: List[str]) -> Tuple[int, int]:
    conn = h.mock.conn
    if not rest:
        if h.method == "GET":
            return h.json(200, partners.listing(conn))
        if h.method == "POST":
            payload = json_body(h.body)
            identifier = payload.pop("id", "")
            if not identifier:
                return h.json(400, {"error": "a partner needs an id"})
            refused = _refused_url(h, payload)
            if refused:
                return h.json(400, {"error": refused})
            try:
                row = partners.create(conn, identifier, payload.pop("name", ""),
                                      **payload)
            except partners.Exists as error:
                return h.json(409, {"error": str(error)})
            except ValueError as error:
                return h.json(400, {"error": str(error)})
            return h.json(201, row)
        return h.text(405, "GET or POST partners")

    identifier = rest[0]
    if rest[1:] == ["profile"]:
        return _profile(h, identifier)
    if h.method == "GET":
        row = partners.get(conn, identifier)
        if row is None:
            return h.json(404, {"error": "no partner %r" % identifier})
        return h.json(200, row)
    if h.method in ("PATCH", "PUT"):
        payload = json_body(h.body)
        refused = _refused_url(h, payload)
        if refused:
            return h.json(400, {"error": refused})
        try:
            row = partners.update(conn, identifier, **payload)
        except partners.UnknownPartner:
            return h.json(404, {"error": "no partner %r" % identifier})
        except ValueError as error:
            return h.json(400, {"error": str(error)})
        return h.json(200, row)
    if h.method == "DELETE":
        outcome = partners.delete(conn, identifier)
        return h.json(200 if outcome["deleted"] else 404, outcome)
    return h.text(405, "GET, PATCH or DELETE a partner")


def _profile(h, identifier: str) -> Tuple[int, int]:
    """A partner's implementation guide: PUT one, GET it, DELETE it.

    Held with the partner, so it survives a restart on a file database.
    A file is loaded the same way: `curl -T guide.json`.
    """
    conn = h.mock.conn
    partner = partners.get(conn, identifier)
    if partner is None:
        return h.json(404, {"error": "no partner %r" % identifier})
    if h.method == "GET":
        found = profiles.load(conn, identifier)
        if found is None:
            return h.json(404, {"error": "%s has no profile; the "
                                         "dictionary applies as it stands"
                                         % identifier})
        return h.json(200, found.as_json())
    if h.method in ("PUT", "POST"):
        try:
            found = profiles.check(partner, json_body(h.body))
        except profiles.Invalid as error:
            return h.json(400, {"error": "profile refused",
                                "problems": error.problems})
        profiles.save(conn, found)
        return h.json(200, found.as_json())
    if h.method == "DELETE":
        removed = profiles.remove(conn, identifier)
        return h.json(200 if removed else 404, {"deleted": removed})
    return h.text(405, "GET, PUT or DELETE a partner's profile")


def _refused_url(h, payload: Dict[str, Any]) -> str:
    """An `as2_url` the courier would refuse to post to, refused now.

    Accepting it and failing every delivery later is the "PATCH answers
    200 and changes nothing" this control plane stopped doing (#40): the
    partner would look configured and nothing would ever arrive.
    """
    url = payload.get("as2_url")
    if not url or not isinstance(url, str):
        return ""
    if delivery.permitted(url, h.config.deliver_to):
        return ""
    return ("as2_url %s is not a host this mock may post to; it was started "
            "with --deliver-to %s" % (url, ",".join(h.config.deliver_to)))


@route(ANY, "/_mock/catalog", rest=True)
def catalog(h, rest: List[str]) -> Tuple[int, int]:
    return h.json(200, db.rows(h.mock.conn, "SELECT * FROM catalog ORDER BY sku"))
