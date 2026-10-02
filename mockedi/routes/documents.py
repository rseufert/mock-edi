"""What has crossed the wire: transaction sets, whole interchanges, and receipts.

Each is registered for `ANY` method with the `rest` of the path read as
the control plane's old `if` chain read it (#182).
"""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

from .. import db
from . import ANY, first, flag, limit, route


@route(ANY, "/_mock/documents", rest=True)
def documents(h, rest: List[str]) -> Tuple[int, int]:
    conn = h.mock.conn
    if rest:
        row = db.one(conn, "SELECT * FROM transaction_set WHERE id = ?",
                     (rest[0],))
        if row is None:
            return h.json(404, {"error": "no document %r" % rest[0]})
        interchange = db.one(conn, "SELECT * FROM interchange WHERE id = ?",
                             (row["interchange_id"],))
        row["findings"] = json.loads(row["findings"] or "[]")
        row["payload"] = interchange["payload"] if interchange else ""
        return h.json(200, row)
    return h.json(200, [
        dict(row, findings=json.loads(row["findings"] or "[]"))
        for row in db.rows(conn, _document_query(h.query),
                           _document_params(h.query))])


@route(ANY, "/_mock/interchanges", rest=True)
def interchanges(h, rest: List[str]) -> Tuple[int, int]:
    conn = h.mock.conn
    if rest:
        row = db.one(conn, "SELECT * FROM interchange WHERE id = ?", (rest[0],))
        if row is None:
            return h.json(404, {"error": "no interchange %r" % rest[0]})
        if flag(h.query, "raw"):
            # The bytes as they arrived or left; a row from before
            # they were kept has only its text.
            raw = row["raw"] if row["raw"] is not None \
                else row["payload"].encode("utf-8")
            content_type = edi_type(row["dialect"])
            if row["charset"]:
                content_type += "; charset=%s" % row["charset"]
            return h.raw(200, bytes(raw), {"Content-Type": content_type})
        row = dict(row)
        row.pop("raw", None)
        return h.json(200, row)
    return h.json(200, db.rows(
        conn, "SELECT id, direction, dialect, partner, control, transport,"
              " message_id, mic, at, length(payload) AS bytes FROM interchange"
              " ORDER BY id DESC LIMIT ?", (limit(h.query),)))


@route(ANY, "/_mock/mdns", rest=True)
def mdns(h, rest: List[str]) -> Tuple[int, int]:
    return h.json(200, db.rows(
        h.mock.conn, "SELECT id, partner, direction, original_id, message_id,"
                     " disposition, mic, mode, url, status, at FROM mdn"
                     " ORDER BY id DESC LIMIT ?", (limit(h.query),)))


def edi_type(dialect: str) -> str:
    return "application/edi-x12" if dialect == "X12" else "application/edifact"


def _document_query(query: Dict[str, List[str]]) -> str:
    clauses = []
    if "acknowledged" in query:
        # `?acknowledged=false` is the useful one: what have we sent that
        # nobody has answered for?
        clauses.append("ack_status %s ''"
                       % ("!=" if flag(query, "acknowledged") else "="))
    if first(query, "direction"):
        clauses.append("direction = ?")
    if first(query, "partner"):
        clauses.append("partner = ?")
    if first(query, "kind"):
        clauses.append("kind = ?")
    if first(query, "code"):
        clauses.append("code = ?")
    if first(query, "reference"):
        clauses.append("reference = ?")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return ("SELECT * FROM transaction_set%s ORDER BY id DESC LIMIT %d"
            % (where, limit(query)))


def _document_params(query: Dict[str, List[str]]) -> List[str]:
    return [first(query, name) for name in
            ("direction", "partner", "kind", "code", "reference")
            if first(query, name)]
