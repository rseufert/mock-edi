"""Who the mock trades with, each one's implementation guide, and what it sells.

A partner and its profile are each registered once for every method they
take, and read `h.method` to tell them apart; another method is refused in
the words they always used.
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from .. import db, delivery, partners, profiles
from . import json_body, route, text


@route("GET", "/_mock/partners", refuse="GET or POST partners")
def listing(h) -> Tuple[int, int]:
    return h.json(200, partners.listing(h.mock.conn))


@route("POST", "/_mock/partners")
def create(h) -> Tuple[int, int]:
    payload = json_body(h.body)
    identifier = text(payload, "id")
    payload.pop("id", None)
    if not identifier:
        return h.json(400, {"error": "a partner needs an id"})
    refused = _refused_url(h, payload)
    if refused:
        return h.json(400, {"error": refused})
    try:
        row = partners.create(h.mock.conn, identifier, payload.pop("name", ""),
                              **payload)
    except partners.Exists as error:
        return h.json(409, {"error": str(error)})
    except ValueError as error:
        return h.json(400, {"error": str(error)})
    return h.json(201, row)


@route("GET", "/_mock/partners/<id>", refuse="GET, PATCH or DELETE a partner")
@route("PATCH", "/_mock/partners/<id>")
@route("PUT", "/_mock/partners/<id>")
@route("DELETE", "/_mock/partners/<id>")
def partner(h, identifier: str) -> Tuple[int, int]:
    conn = h.mock.conn
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
    outcome = partners.delete(conn, identifier)
    return h.json(200 if outcome["deleted"] else 404, outcome)


@route("GET", "/_mock/partners/<id>/profile",
       refuse="GET, PUT or DELETE a partner's profile")
@route("PUT", "/_mock/partners/<id>/profile")
@route("POST", "/_mock/partners/<id>/profile")
@route("DELETE", "/_mock/partners/<id>/profile")
def profile(h, identifier: str) -> Tuple[int, int]:
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
    removed = profiles.remove(conn, identifier)
    return h.json(200 if removed else 404, {"deleted": removed})


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


@route("GET", "/_mock/catalog")
def catalog(h) -> Tuple[int, int]:
    return h.json(200, db.rows(h.mock.conn, "SELECT * FROM catalog ORDER BY sku"))
