"""Posting documents to a partner that has somewhere to receive them.

A partner with no `as2_url` is a mailbox: documents sit in the queue until
something collects them over `/_mock/mailbox`.  That is the right default,
because the common case is a test that drives the mock and reads back what it
produced.

A partner *with* an `as2_url` is the other half of the point.  Set one to your
own AS2 listener and the mock stops being something you poll and starts being
something that arrives - which is the only way to test the code that runs when
an 856 turns up unannounced at four in the morning.

Delivery happens on one background thread.  Not a thread per document: a mock
that answers an 850 by opening four sockets at once is a good way to discover
that your listener is not thread safe, but a bad way to discover anything else,
so documents go out in the order they were queued.

A courier can be *held* (`--hold-delivery`). It then posts nothing of its own
accord: what it is handed waits, in the same order, until `step` sends the
head of the queue and says what became of it. Two mocks wired to each other
finish a whole rally inside one `settle()` otherwise, and nothing outside can
read the state between two hops of it (#271).
"""
from __future__ import annotations

import collections
import datetime
import email.utils
import http.client
import json
import queue
import sqlite3
import threading
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence

from . import as2, db

DEFAULT_TIMEOUT = 10.0


class Courier:
    """Delivers released documents and asynchronous MDNs, in the background."""

    def __init__(self, pipeline, timeout: float = DEFAULT_TIMEOUT,
                 opener=None, held: bool = False):
        self.pipeline = pipeline
        # Held, nothing is posted until `step` asks for it. What would have
        # gone on the queue waits here instead, in the order it would have
        # gone, so a step sends what an unheld courier would have sent next.
        self.held = held
        self._held: "collections.deque[tuple]" = collections.deque()
        self._held_guard = threading.Lock()
        self.config = pipeline.config
        self.timeout = timeout
        # Injected in tests so that delivery can be exercised without a
        # listener on the other end.
        self.opener = opener or _post
        # Replaced by the server with the mock's own lock, so that the courier
        # and the request handlers do not interleave read-modify-write work on
        # the database. It is held around the database calls only: a partner
        # that takes ten seconds to answer must not stall the whole mock.
        self.lock = threading.RLock()
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.failures: List[str] = []

    def forget(self) -> None:
        """Drop what the courier remembers of past deliveries, for a reset."""
        with self.lock:
            self.failures.clear()
        with self._held_guard:
            self._held.clear()

    # -- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._work, daemon=True,
                                        name="mock-edi-courier")
        self._thread.start()

    def stop(self, wait: float = 2.0) -> bool:
        """Ask the courier to stop, and say whether it did within `wait`.

        A delivery already under way is a network call that cannot be
        interrupted, so the answer can be no. The caller has to know: closing
        the database under a thread that is still running is how the mock
        once crashed the interpreter rather than raising.
        """
        self._stop.set()
        self._queue.put(("stop", 0))
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=wait)
        if thread.is_alive():
            return False
        self._thread = None
        return True

    def drain(self, timeout: float = 5.0) -> None:
        """Wait for the queue to empty. Tests use it instead of sleeping."""
        deadline = threading.Event()
        self._queue.put(("flush", deadline))
        deadline.wait(timeout)

    # -- work

    def enqueue_many(self, outbound_ids: List[int]) -> None:
        if self.held:
            with self._held_guard:
                self._held.extend(("document", int(item)) for item in outbound_ids)
            return
        for item in outbound_ids:
            self._queue.put(("document", int(item)))
        self.start()

    def enqueue_mdn(self, mdn_id: int) -> None:
        if self.held:
            with self._held_guard:
                self._held.append(("mdn", int(mdn_id)))
            return
        self._queue.put(("mdn", int(mdn_id)))
        self.start()

    # -- one at a time, when held

    def step(self) -> Optional[Dict[str, Any]]:
        """Send the next thing a held courier is holding, and say what it was.

        None when there is nothing to send. The send itself happens where
        every send does, on the courier's thread, and this waits for that one
        to finish - so the caller must not be holding the mock's lock, which
        the courier needs for its database work. A mock that posts to itself
        would otherwise wait for an answer only it can give.

        What the courier would not send is passed over, not counted: a
        document for a partner that collects from the mailbox, and one that
        is no longer waiting because it was collected or cancelled meanwhile.
        """
        while True:
            with self._held_guard:
                if not self._held:
                    return None
                kind, identifier = self._held.popleft()
            with self.lock:
                if not self._would_send(kind, identifier):
                    continue
            done = threading.Event()
            self._queue.put((kind, identifier))
            self._queue.put(("flush", done))
            self.start()
            # The send is bounded by the courier's own timeout; the margin is
            # for the thread to come round to it.
            done.wait(self.timeout + 5.0)
            with self.lock:
                return self._moved(kind, identifier)

    def waiting(self) -> int:
        """How many held things a step would still send."""
        with self._held_guard:
            held = list(self._held)
        with self.lock:
            return sum(1 for kind, identifier in held
                       if self._would_send(kind, identifier))

    def _would_send(self, kind: str, identifier: int) -> bool:
        conn = self.pipeline.conn
        if kind == "mdn":
            row = db.one(conn, "SELECT status, url FROM mdn WHERE id = ?",
                         (identifier,))
            return bool(row and row["status"] == "pending" and row["url"])
        row = db.one(conn,
                     "SELECT outbound.status, partner.as2_url FROM outbound"
                     " JOIN partner ON partner.id = outbound.partner"
                     " WHERE outbound.id = ?", (identifier,))
        return bool(row and row["status"] == "ready" and row["as2_url"])

    def _moved(self, kind: str, identifier: int) -> Dict[str, Any]:
        """What a step sent and how it ended, as the outbox or `/_mock/mdns` has it."""
        conn = self.pipeline.conn
        if kind == "mdn":
            row = db.one(conn, "SELECT * FROM mdn WHERE id = ?", (identifier,))
            return {"type": "mdn", "id": identifier, "partner": row["partner"],
                    "originalId": row["original_id"],
                    "messageId": row["message_id"], "to": row["url"],
                    "status": row["status"]}
        row = db.one(conn, "SELECT * FROM outbound WHERE id = ?", (identifier,))
        return {"type": "document", "id": identifier, "partner": row["partner"],
                "code": row["code"], "kind": row["kind"],
                "reference": row["reference"], "control": row["control"],
                "messageId": row["message_id"], "to": row["delivery"],
                "status": row["status"], "note": row["note"]}

    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                kind, payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if kind == "stop":
                    return
                if kind == "flush":
                    payload.set()
                elif kind == "document":
                    self.deliver(payload)
                elif kind == "mdn":
                    self.deliver_mdn(payload)
            except Exception as error:  # pragma: no cover - a courier must not die
                self.failures.append("%s %s: %s" % (kind, payload, error))
            finally:
                self._queue.task_done()

    # -- the deliveries themselves

    def deliver(self, outbound_id: int) -> bool:
        """POST one released document to its partner's AS2 URL."""
        conn = self.pipeline.conn
        with self.lock:
            row = db.one(conn, "SELECT * FROM outbound WHERE id = ?", (outbound_id,))
            if row is None or row["status"] != "ready":
                return False
            partner = db.one(conn, "SELECT * FROM partner WHERE id = ?",
                             (row["partner"],))
            if partner is None or not partner["as2_url"]:
                return False   # a mailbox partner: leave it to be collected
            if not permitted(partner["as2_url"], self.config.deliver_to):
                _finish(conn, outbound_id, "failed", partner["as2_url"],
                        "refused: %s is not in --deliver-to"
                        % partner["as2_url"])
                return False
            body, _charset = self.pipeline.wire(row)

        headers = as2.outbound_headers(
            self.config.as2_id, partner["id"],
            "%s %s" % (row["code"], row["reference"]), row["dialect"],
            row["message_id"], request_mdn=True)
        try:
            status, response_headers, response = self.opener(
                partner["as2_url"], headers, body, self.timeout)
        except Exception as error:
            with self.lock:
                _finish(conn, outbound_id, "failed", partner["as2_url"],
                        "delivery failed: %s" % error)
            return False

        with self.lock:
            if status >= 400:
                _finish(conn, outbound_id, "failed", partner["as2_url"],
                        "the partner answered HTTP %d" % status)
                return False
            note = "delivered"
            if response:
                fields = as2.parse_mdn(response)
                if fields.get("Disposition"):
                    note = "MDN: %s" % fields["Disposition"]
                    _record_mdn(conn, partner["id"], "in", row["message_id"],
                                fields, response.decode("utf-8", "replace"))
            _finish(conn, outbound_id, "delivered", partner["as2_url"], note)
        return True

    def deliver_mdn(self, mdn_id: int) -> bool:
        """POST an asynchronous MDN back to the URL the sender named."""
        conn = self.pipeline.conn
        with self.lock:
            row = db.one(conn, "SELECT * FROM mdn WHERE id = ?", (mdn_id,))
            if row is None or row["status"] != "pending" or not row["url"]:
                return False
            if not permitted(row["url"], self.config.deliver_to):
                conn.execute("UPDATE mdn SET status = ? WHERE id = ?",
                             ("refused", mdn_id))
                conn.commit()
                return False
        headers = _mdn_headers(row, self.config.as2_id)
        try:
            status, _headers, _body = self.opener(
                row["url"], headers, row["payload"].encode("utf-8"), self.timeout)
            ok = status < 400
            note = "sent" if ok else "the partner answered HTTP %d" % status
        except Exception as error:
            ok, note = False, "delivery failed: %s" % error
        with self.lock:
            conn.execute("UPDATE mdn SET status = ? WHERE id = ?",
                         ("sent" if ok else "failed", mdn_id))
            conn.commit()
        if not ok:
            self.failures.append("mdn %d: %s" % (mdn_id, note))
        return ok


def _post(url: str, headers: Dict[str, str], body: bytes, timeout: float):
    """POST with the header names spelled exactly as they were given.

    `urllib` re-cases header names on the way out, so `AS2-From` leaves as
    `As2-From`. HTTP says that is the same header, and a correct receiver
    treats it as such - but AS2 implementations that match the name literally
    do exist, and a mock whose job is to look like a real trading partner
    should not be the one sending the unusual spelling. `http.client` puts
    the name on the wire as written.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("an AS2 URL must be http or https, not %r" % parts.scheme)
    connection_class = (http.client.HTTPSConnection if parts.scheme == "https"
                        else http.client.HTTPConnection)
    connection = connection_class(parts.netloc, timeout=timeout)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    try:
        connection.putrequest("POST", target, skip_accept_encoding=True)
        for key, value in headers.items():
            connection.putheader(key, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders()
        connection.send(body)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _finish(conn: sqlite3.Connection, outbound_id: int, status: str,
            url: str, note: str) -> None:
    """Record what became of one delivery attempt.

    `attempts` counts them all, successful or not, so that a row redelivered
    after a failure says so rather than looking as though it went first time.
    `last_error` is kept only while there is one: a retry that succeeds
    clears it, and the note carries the outcome either way.
    """
    conn.execute(
        "UPDATE outbound SET status = ?, delivered_at = ?, delivery = ?, note = ?,"
        " attempts = attempts + 1, last_attempt_at = ?, last_error = ?"
        " WHERE id = ?",
        (status, db.now(conn), url, note, db.now(conn),
         note if status == "failed" else "", outbound_id))
    conn.commit()


def permitted(url: str, allowed: Sequence[str]) -> bool:
    """Whether the courier may post to this URL.

    An empty allowlist permits everything, which is what a laptop wants. With
    one configured, the URL's host must be on it: a mock reachable from a
    network posts to whatever `as2_url` a partner carries and to whatever
    `Receipt-Delivery-Option` an unauthenticated AS2 sender names, so without
    this it can be asked to post stored payloads at an internal address.

    The host is compared without its port, case-folded, and a URL that cannot
    be parsed into a host is refused rather than given the benefit of the
    doubt.
    """
    if not allowed:
        return True
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and host in {item.strip().lower() for item in allowed
                                   if item.strip()}


def _mdn_headers(row, as2_id: str) -> Dict[str, str]:
    """The headers to post an asynchronous MDN with.

    The ones it was built with, which carry the MIME boundary. Without that
    parameter a `multipart/report` cannot be parsed by any MIME library, so
    the partner logs a malformed MDN and the message it acknowledged stays
    outstanding on their side.

    A row written before those headers were kept - by an older version, into
    a file database - has the boundary in its body and nowhere else, so it is
    read back from there rather than guessed at.
    """
    try:
        stored = json.loads(row["headers"] or "{}")
    except (ValueError, IndexError, KeyError):
        stored = {}
    if stored:
        return dict(stored)

    content_type = "multipart/report; report-type=disposition-notification"
    boundary = _boundary_of(row["payload"])
    if boundary:
        content_type += '; boundary="%s"' % boundary
    return {
        "Content-Type": content_type,
        "AS2-Version": as2.AS2_VERSION,
        "AS2-From": as2_id,
        "AS2-To": row["partner"],
        "Message-ID": row["message_id"],
        "MIME-Version": "1.0",
        "Date": email.utils.format_datetime(db.utcnow()),
    }


def _boundary_of(payload: str) -> str:
    """The MIME boundary a rendered multipart body announces in its first line."""
    for line in (payload or "").splitlines():
        if line.startswith("--") and not line.endswith("--"):
            return line[2:].strip()
    return ""


def _record_mdn(conn: sqlite3.Connection, partner: str, direction: str,
                original_id: str, fields: Dict[str, str], payload: str) -> None:
    conn.execute(
        "INSERT INTO mdn (partner, direction, original_id, message_id,"
        " disposition, mic, mode, status, payload, at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (partner, direction, original_id, fields.get("Original-Message-ID", ""),
         fields.get("Disposition", ""), fields.get("Received-Content-MIC", ""),
         "sync", "received", payload, db.now(conn)))
    conn.commit()
