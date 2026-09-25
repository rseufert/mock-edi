"""Which character set a document is in, and turning it into bytes and back.

Real EDI is mostly not UTF-8.  An EDIFACT interchange says what it is in UNB
S001 - `UNOC`, the identifier the mock itself writes, *is* ISO 8859-1 - and
X12 says nothing at all, so a Windows translator's cp1252 or Latin-1 arrives
undeclared.  Decoding everything as UTF-8 turned `Müller` into `M�ller` and,
worse, made the archive disagree with the bytes that were received.

So the two jobs are kept apart:

* **Storing** keeps the bytes.  What arrived is what `?raw` returns, whatever
  it was encoded in.
* **Reading** decodes by what the document declares: UNB S001 for EDIFACT;
  for X12 the HTTP `charset` if the sender gave one, and otherwise ISO
  8859-1, which cannot fail and is what an X12 translator most often used.

Cutting a payload into interchanges happens before either, over the payload
viewed as ISO 8859-1: that maps every byte to one character and back, so each
interchange's own bytes are recovered exactly.  Every delimiter is ASCII, and
no byte of a UTF-8 multi-byte character is, so the cut is safe there too.
"""
from __future__ import annotations

import codecs
import re
import sqlite3
from typing import Optional

# Lossless and never fails: the fallback, and the view used for cutting.
BYTES = "iso-8859-1"

# ISO 9735 syntax identifiers (UNB S001/0001) and the character sets they
# name. UNOA and UNOB are subsets of ASCII. An identifier not listed here is
# read as ISO 8859-1, which loses nothing, rather than refused.
EDIFACT_SYNTAX = {
    "UNOA": "ascii", "UNOB": "ascii",
    "UNOC": "iso-8859-1", "UNOD": "iso-8859-2", "UNOE": "iso-8859-5",
    "UNOF": "iso-8859-7", "UNOG": "iso-8859-3", "UNOH": "iso-8859-4",
    "UNOI": "iso-8859-6", "UNOJ": "iso-8859-8", "UNOK": "iso-8859-9",
    "UNOY": "utf-8",
}

_SYNTAX = re.compile(r"UNB\s*.(UNO[A-Z])")
_CHARSET = re.compile(r"charset\s*=\s*\"?([A-Za-z0-9_.:-]+)", re.I)


def _known(name: str) -> Optional[str]:
    try:
        return codecs.lookup(name).name
    except (LookupError, TypeError):
        return None


def from_content_type(content_type: str) -> str:
    """The `charset` parameter of a Content-Type, if it names a real one."""
    found = _CHARSET.search(content_type or "")
    return (_known(found.group(1)) or "") if found else ""


def declared(dialect: str, raw: bytes, http_charset: str = "") -> str:
    """The character set one interchange is in.

    EDIFACT declares it, and that outranks what HTTP says. X12 does not, so
    the HTTP charset is the only evidence there is.
    """
    if dialect == "EDIFACT":
        found = _SYNTAX.search(raw[:512].decode(BYTES))
        if found:
            return EDIFACT_SYNTAX.get(found.group(1), BYTES)
        return BYTES
    return _known(http_charset) or BYTES


def decode(raw: bytes, charset: str) -> str:
    """Text to read the document by. A byte its charset does not allow is
    replaced for reading; the stored bytes keep it."""
    return raw.decode(charset, "replace")


def encode(text: str, charset: str) -> bytes:
    """Bytes to send. A character the charset cannot carry becomes `?`, which
    is what a translator writing to that charset does."""
    return text.encode(charset, "replace")


def for_outbound(conn: sqlite3.Connection, partner: str, dialect: str,
                 payload: str) -> str:
    """The character set to send a document to `partner` in.

    EDIFACT: the one its own UNB declares. X12: the one the partner's last
    interchange arrived in, so that an 855 answering a Latin-1 850 can be read
    by the translator that sent the 850.
    """
    if dialect == "EDIFACT":
        return declared(dialect, payload[:512].encode(BYTES, "replace"))
    row = conn.execute(
        "SELECT charset FROM interchange WHERE direction = 'in' AND partner = ?"
        " AND dialect = ? AND charset != '' ORDER BY id DESC LIMIT 1",
        (partner, dialect)).fetchone()
    return (row[0] if row else "") or BYTES
