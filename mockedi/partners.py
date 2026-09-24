"""Who the mock trades with, and how each of them misbehaves.

A trading partner in this mock is a row: an identifier that appears in ISA06
or UNB's S002, a dialect, and a *behaviour*.  The behaviour is the reason the
project exists.  Anyone can stand up something that answers an 850 correctly;
what is hard to get hold of is a partner that short-ships on Tuesdays, or
sends the invoice twice, or silently never acknowledges anything - and those
are the cases your error handling was written for and has never been run
against.

Behaviours are changed at runtime through `/_mock/partners/<id>`, so a test
reproduces a specific failure without restarting anything.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from . import db
from .transactions import Party

BEHAVIOURS = db.BEHAVIOURS

# Behaviours that change what the *documents* say, as opposed to whether they
# are sent at all.
DOCUMENT_BEHAVIOURS = ("accept", "short-ship", "reject-line", "reject-all")


class UnknownPartner(KeyError):
    """An interchange arrived from an identifier the mock does not trade with.

    Real AS2 servers refuse these, and so does this one: accepting anything
    that turns up would hide the single most common AS2 misconfiguration,
    which is an AS2-From that does not match what the other side registered.
    """


def get(conn: sqlite3.Connection, identifier: str) -> Optional[Dict[str, Any]]:
    return db.one(conn, "SELECT * FROM partner WHERE id = ?", (identifier,))


def require(conn: sqlite3.Connection, identifier: str) -> Dict[str, Any]:
    row = get(conn, identifier)
    if row is None:
        raise UnknownPartner(identifier)
    return row


def listing(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return db.rows(conn, "SELECT * FROM partner ORDER BY id")


def create(conn: sqlite3.Connection, identifier: str, name: str = "",
           **fields: Any) -> Dict[str, Any]:
    columns = {
        "qualifier": "ZZ", "dialect": "X12", "version": "004010",
        "behaviour": "accept", "as2_url": "", "mdn_mode": "sync",
        "street": "", "city": "", "region": "", "postal": "", "country": "US",
        "duns": "", "test": 1,
    }
    columns.update({k: v for k, v in fields.items() if k in columns})
    if columns["dialect"] not in ("X12", "EDIFACT"):
        raise ValueError("dialect must be X12 or EDIFACT, not %r" % columns["dialect"])
    if columns["behaviour"] not in BEHAVIOURS:
        raise ValueError("unknown behaviour %r; known: %s"
                         % (columns["behaviour"], ", ".join(sorted(BEHAVIOURS))))
    keys = ["id", "name"] + sorted(columns)
    values = [identifier, name or identifier] + [columns[k] for k in sorted(columns)]
    conn.execute("INSERT OR REPLACE INTO partner (%s) VALUES (%s)"
                 % (", ".join(keys), ", ".join("?" * len(keys))), values)
    conn.commit()
    return require(conn, identifier)


def update(conn: sqlite3.Connection, identifier: str,
           **fields: Any) -> Dict[str, Any]:
    row = require(conn, identifier)
    allowed = set(row) - {"id"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if "behaviour" in changes and changes["behaviour"] not in BEHAVIOURS:
        raise ValueError("unknown behaviour %r; known: %s"
                         % (changes["behaviour"], ", ".join(sorted(BEHAVIOURS))))
    if "dialect" in changes and changes["dialect"] not in ("X12", "EDIFACT"):
        raise ValueError("dialect must be X12 or EDIFACT")
    if not changes:
        return row
    conn.execute("UPDATE partner SET %s WHERE id = ?"
                 % ", ".join("%s = ?" % k for k in changes),
                 list(changes.values()) + [identifier])
    conn.commit()
    return require(conn, identifier)


def delete(conn: sqlite3.Connection, identifier: str) -> bool:
    cursor = conn.execute("DELETE FROM partner WHERE id = ?", (identifier,))
    conn.commit()
    return cursor.rowcount > 0


def us(config) -> Party:
    """The mock's own party record, as it appears in the documents it sends."""
    return Party(role="SE", name=config.name, identifier=config.as2_id,
                 street=config.street, city=config.city, region=config.region,
                 postal=config.postal, country=config.country)
