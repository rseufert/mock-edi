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

import re
import sqlite3
import urllib.parse
from typing import Any, Dict, List, Optional

from . import db
from .transactions import Party

# An outbound document that will never be sent, because the partner it was
# addressed to has gone.
CANCELLED = "cancelled"

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


# Every column a caller may set, with its default. Anything not named here is
# refused rather than dropped: a PATCH that silently ignores a misspelled
# field sends whoever wrote it looking for the bug somewhere else entirely.
FIELDS = {
    "name": "", "qualifier": "ZZ", "dialect": "X12", "version": "004010",
    "behaviour": "accept", "as2_url": "", "mdn_mode": "sync",
    "street": "", "city": "", "region": "", "postal": "", "country": "US",
    "duns": "", "test": 1,
}

DIALECTS = ("X12", "EDIFACT")
MDN_MODES = ("sync", "async")

# What the wire can carry, per dialect.
#
#   X12      ISA06/ISA08 are fixed at 15 characters and ISA05/ISA07 at 2, so
#            anything longer is truncated and the partner becomes unreachable
#            by its own identifier. GS08 is a six-character version code.
#   EDIFACT  UNB's S002/0004 is up to 35 and its qualifier up to 4. The
#            message version is a directory: D:96A:UN.
LIMITS = {
    "X12": {"id": 15, "qualifier": 2, "version": re.compile(r"^\d{6}$"),
            "version_hint": "six digits, e.g. 004010 or 005010"},
    "EDIFACT": {"id": 35, "qualifier": 4,
                "version": re.compile(r"^D:\d{2}[A-Z]:UN$"),
                "version_hint": "a directory, e.g. D:96A:UN"},
}


# The characters an id may use: letters, digits, and `.`, `-` and `_` between
# them. Narrower than either standard - X12's basic set allows `/`, for one -
# and on purpose: an id also becomes a pickup filename and a path in the
# control plane's URLs, and it must not carry a delimiter onto the wire. So no
# `/` or `\`, no leading dot, no spaces, and nothing a translator treats as
# punctuation.
ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


class Invalid(ValueError):
    """A partner field the mock would not be able to act on."""


def _known_fields() -> str:
    return ", ".join(sorted(FIELDS))


def check(fields: Dict[str, Any], dialect: str,
          identifier: str = "") -> Dict[str, Any]:
    """Validate and normalise the fields of a partner.

    Returns the fields with the values the database should hold; raises
    `Invalid` naming what is wrong and, where there is a fixed set, what
    would have been accepted.
    """
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise Invalid("unknown field%s %s; a partner has: %s"
                      % ("s" if len(unknown) > 1 else "",
                         ", ".join(repr(u) for u in unknown), _known_fields()))

    out = dict(fields)
    if "dialect" in out:
        dialect = out["dialect"]
        if dialect not in DIALECTS:
            raise Invalid("dialect must be one of %s, not %r"
                          % (" or ".join(DIALECTS), dialect))
    limits = LIMITS[dialect if dialect in LIMITS else "X12"]

    if "behaviour" in out and out["behaviour"] not in BEHAVIOURS:
        raise Invalid("unknown behaviour %r; known: %s"
                      % (out["behaviour"], ", ".join(sorted(BEHAVIOURS))))

    if "version" in out and not limits["version"].match(str(out["version"])):
        raise Invalid("%s version %r is not one the mock can write: %s"
                      % (dialect, out["version"], limits["version_hint"]))

    if "mdn_mode" in out and out["mdn_mode"] not in MDN_MODES:
        raise Invalid("mdn_mode must be one of %s, not %r"
                      % (" or ".join(MDN_MODES), out["mdn_mode"]))

    if "test" in out:
        out["test"] = _flag(out["test"])

    if "as2_url" in out and out["as2_url"]:
        parsed = urllib.parse.urlsplit(str(out["as2_url"]))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise Invalid("as2_url must be an http or https URL with a host, "
                          "not %r" % out["as2_url"])

    if "qualifier" in out:
        width = len(str(out["qualifier"]))
        if not 1 <= width <= limits["qualifier"]:
            raise Invalid("qualifier %r is %d characters; %s allows at most %d"
                          % (out["qualifier"], width, dialect,
                             limits["qualifier"]))

    if identifier:
        _check_id(identifier, dialect, limits)
    return out


def _check_id(identifier: str, dialect: str, limits) -> None:
    if not identifier.strip():
        raise Invalid("a partner needs an id")
    if not ID_PATTERN.match(identifier):
        raise Invalid(
            "id %r may use only letters, digits, and '.', '-' or '_' between "
            "them: it goes on the wire, into a pickup filename and into URLs"
            % identifier)
    if len(identifier) > limits["id"]:
        raise Invalid(
            "id %r is %d characters; %s carries at most %d, and a longer one "
            "is truncated on the wire, leaving the partner unable to be found "
            "by the id it sends" % (identifier, len(identifier), dialect,
                                    limits["id"]))


def _flag(value: Any) -> int:
    """`test` is a flag: accept what a JSON client plausibly sends, refuse prose."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str) and value.strip() in ("0", "1"):
        return int(value.strip())
    raise Invalid("test is a flag: 0 or 1, not %r" % value)


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
    """Register a partner, refusing anything the mock could not then act on."""
    given = dict(fields)
    if name:
        given["name"] = name
    checked = check(given, given.get("dialect", FIELDS["dialect"]),
                    identifier=identifier)

    columns = dict(FIELDS)
    columns.update(checked)
    columns["name"] = columns["name"] or identifier

    keys = ["id"] + sorted(columns)
    values = [identifier] + [columns[k] for k in sorted(columns)]
    conn.execute("INSERT OR REPLACE INTO partner (%s) VALUES (%s)"
                 % (", ".join(keys), ", ".join("?" * len(keys))), values)
    conn.commit()
    return require(conn, identifier)


def update(conn: sqlite3.Connection, identifier: str,
           **fields: Any) -> Dict[str, Any]:
    """Change a partner, refusing anything the mock could not then act on.

    An id cannot be changed - it is what an arriving interchange is matched
    on - but a *dialect* can, and that narrows what the id may be: moving a
    30-character EDIFACT partner to X12 would leave it unreachable, so it is
    refused rather than quietly broken.
    """
    row = require(conn, identifier)
    if not fields:
        return row
    changes = check(fields, row["dialect"])
    if "dialect" in changes and changes["dialect"] != row["dialect"]:
        _check_id(identifier, changes["dialect"], LIMITS[changes["dialect"]])

    conn.execute("UPDATE partner SET %s WHERE id = ?"
                 % ", ".join("%s = ?" % k for k in changes),
                 list(changes.values()) + [identifier])
    conn.commit()
    return require(conn, identifier)


def delete(conn: sqlite3.Connection, identifier: str) -> Dict[str, Any]:
    """Remove a partner, and the work that was still owed to it.

    A partner with documents waiting and promises outstanding cannot simply
    vanish: the mailbox would go on offering documents addressed to somebody
    the mock no longer trades with, and the scheduler would go on packing
    shipments for them. Both are stopped and *marked*, not deleted - what the
    mock did before the partner went away is still the evidence a test needs.
    """
    if get(conn, identifier) is None:
        return {"deleted": False, "cancelled": 0, "unscheduled": 0}

    cancelled = conn.execute(
        "UPDATE outbound SET status = ?, note = ? WHERE partner = ?"
        " AND status IN ('pending', 'ready')",
        (CANCELLED, "partner deleted", identifier)).rowcount
    unscheduled = conn.execute(
        "UPDATE scheduled SET done_at = ?, note = ? WHERE partner = ?"
        " AND done_at = ''",
        (db.now(), "partner deleted", identifier)).rowcount
    conn.execute("DELETE FROM partner WHERE id = ?", (identifier,))
    conn.commit()
    return {"deleted": True, "cancelled": int(cancelled),
            "unscheduled": int(unscheduled)}


def us(config) -> Party:
    """The mock's own party record, as it appears in the documents it sends."""
    return Party(role="SE", name=config.name, identifier=config.as2_id,
                 street=config.street, city=config.city, region=config.region,
                 postal=config.postal, country=config.country)
