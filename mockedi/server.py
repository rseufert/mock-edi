"""The HTTP front end: AS2, the plain EDI endpoint, and the control plane.

Three ways in, on purpose:

* `POST /as2` - the real thing, with AS2 headers and an MDN back.  Use this
  when what you are testing is your AS2 client.
* `POST /edi` - the same pipeline with none of the ceremony, answering with a
  JSON summary of what the mock made of the document.  Use this when what you
  are testing is your *mapping*, and AS2 is just in the way.
* `/_mock/...` - the control plane.  Read what happened, change how a partner
  behaves, release queued documents, reset.

The control plane is the part that makes failures reproducible.  Telling a
partner to short-ship is one PATCH; getting a real trading partner to do it on
demand is a support ticket and a fortnight.
"""
from __future__ import annotations

import datetime
import json
import math
import random
import socketserver
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

from . import (as2, charsets, db, delivery, documents, drop, partners, pipeline,
               reconcile, schema, transactions, validate)
from .envelope import EdiSyntaxError

JSON = "application/json; charset=utf-8"
TEXT = "text/plain; charset=utf-8"
HTML = "text/html; charset=utf-8"
ALLOWED_METHODS = "GET, HEAD, POST, PATCH, PUT, DELETE, OPTIONS"


@dataclass
class Config:
    """Everything the mock can be told, in one place."""
    host: str = "127.0.0.1"
    port: int = 8080
    db_path: str = ":memory:"

    # Who the mock is, as it appears in ISA06 / UNB S002 and in AS2-To.
    as2_id: str = "MOCKEDI"
    name: str = "Mock EDI Supply Co"
    qualifier: str = "ZZ"
    street: str = "500 Seaport Boulevard"
    city: str = "Boston"
    region: str = "MA"
    postal: str = "02210"
    country: str = "US"

    # How it behaves.
    strict_receiver: bool = True
    pretty: bool = True
    tax_rate: str = "0"
    ack_delay_ms: int = 0
    response_delay_ms: int = 0
    despatch_delay_ms: int = 0
    invoice_delay_ms: int = 0
    mdn: bool = True
    deliver_timeout: float = 10.0
    # The largest request body read, and how long a request may take to
    # arrive. Every payload is stored, so the cap protects a --db file too.
    max_body_bytes: int = 16 * 1024 * 1024
    request_timeout: float = 60.0
    # Accept an interchange control number a partner has already used. Off by
    # default: a real receiver refuses the replay rather than shipping twice.
    allow_duplicates: bool = False

    # Trading over a directory instead of over HTTP.
    drop_dir: str = ""
    pickup_dir: str = ""
    drop_interval_ms: int = 1000
    drop_settle_ms: int = 250

    # Testing knobs.
    basic_auth: Optional[str] = None
    seed_value: int = 42
    latency_ms: int = 0
    error_rate: float = 0.0
    log_requests: bool = True
    quiet: bool = False


class Mock:
    """The mock's state: a database, a pipeline, and a courier."""

    def __init__(self, config: Config):
        self.config = config
        self.conn = db.connect(config.db_path)
        db.seed(self.conn, config.seed_value, config.as2_id)
        self.pipeline = pipeline.Pipeline(self.conn, config)
        self.courier = delivery.Courier(self.pipeline, config.deliver_timeout)
        self.dropbox = drop.DropBox(
            self.pipeline, config.drop_dir, config.pickup_dir,
            config.drop_settle_ms, config.drop_interval_ms)
        self.pipeline.on_release = self._released
        # One lock over everything that touches the database. SQLite itself
        # copes with several threads on one connection, but the mock's
        # read-modify-write sequences do not - two interchanges arriving at
        # once must not be handed the same ISA13. The courier and the drop
        # poller hold the same lock, and release it before a network call or
        # a file write.
        self.lock = threading.RLock()
        self.courier.lock = self.lock
        self.dropbox.lock = self.lock
        self.started = datetime.datetime.now()
        self.random = random.Random(config.seed_value)

    def _released(self, outbound_ids) -> None:
        """A document became ready: post it, write it, or leave it to be collected.

        The three are not alternatives. A partner can have an AS2 URL *and* a
        pickup directory, and a document nobody takes stays in the mailbox
        either way.
        """
        self.dropbox.write(outbound_ids)
        self.courier.enqueue_many(outbound_ids)

    def resume(self) -> Dict[str, int]:
        """Pick up the deliveries a previous run left unfinished.

        The courier learns of work only when it is released, so on a file
        database a restart left documents `ready` for an AS2 partner - and
        asynchronous MDNs `pending` - with nobody to post them. They go back
        on the queue in the order they were first queued. Documents for a
        partner with no AS2 URL are waiting to be collected, not delivered,
        and stay where they are.
        """
        with self.lock:
            documents = [row["id"] for row in db.rows(
                self.conn,
                "SELECT outbound.id FROM outbound JOIN partner"
                " ON partner.id = outbound.partner"
                " WHERE outbound.status = 'ready' AND partner.as2_url != ''"
                " ORDER BY outbound.id")]
            receipts = [row["id"] for row in db.rows(
                self.conn,
                "SELECT id FROM mdn WHERE direction = 'out' AND mode = 'async'"
                " AND status = 'pending' AND url != '' ORDER BY id")]
        if documents:
            self.courier.enqueue_many(documents)
        for mdn_id in receipts:
            self.courier.enqueue_mdn(mdn_id)
        return {"documents": len(documents), "mdns": len(receipts)}

    def close(self, wait: float = 2.0) -> List[str]:
        """Stop the background threads and close the database.

        The connection is closed under the lock every thread takes around
        its database work, so no thread can be inside a query when it goes:
        closing SQLite under a running statement is a use-after-free in C,
        and it crashes the interpreter instead of raising. A thread that
        outlives `wait` is named, here and on stderr, rather than ignored;
        when it next takes the lock it finds the connection closed, which is
        an ordinary exception.

        Returns the threads that would not stop.
        """
        stuck = [name for name, worker in (("drop poller", self.dropbox),
                                           ("courier", self.courier))
                 if not worker.stop(wait)]
        if stuck:
            sys.stderr.write("mock-edi: the %s did not stop within %.1fs; "
                             "closing the database once it lets go\n"
                             % (" and the ".join(stuck), wait))
        with self.lock:
            self.conn.close()
        return stuck

    def reset(self) -> None:
        """Back to a freshly seeded system, without restarting the process."""
        with self.lock:
            for table in ("interchange", "transaction_set", "purchase_order",
                          "order_line", "shipment", "shipment_line", "invoice",
                          "outbound", "scheduled", "mdn", "control_number",
                          "request_log",
                          "partner", "catalog"):
                self.conn.execute("DELETE FROM %s" % table)
            self.conn.commit()
            db.seed(self.conn, self.config.seed_value, self.config.as2_id)
            self.pipeline.offset = datetime.timedelta(0)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

class BodyError(Exception):
    """A request body that cannot be read, and the status that says why.

    Every one of these closes the connection: a body that was not read to its
    end leaves bytes on the socket that would otherwise be parsed as the next
    request.
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-edi"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        # A socket timeout, so a client that sends headers and then nothing
        # is let go of rather than holding a thread for as long as it likes.
        self.timeout = self.server.mock.config.request_timeout or None
        super().setup()

    # -- plumbing

    @property
    def mock(self) -> Mock:
        return self.server.mock

    @property
    def config(self) -> Config:
        return self.server.mock.config

    def log_message(self, fmt, *args):
        if not self.config.quiet:
            sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_HEAD(self):
        # A GET without the body: what a load balancer's or an orchestrator's
        # liveness probe sends to /_mock/health.
        self._handle("HEAD")

    def do_OPTIONS(self):
        # Answered before authentication, as a CORS preflight never carries
        # credentials. The mock sends no CORS headers of its own.
        path, _query = _split(self.path)
        try:
            # Read and discarded: on a kept-alive connection, a body left
            # unread would be taken for the next request.
            self._read_body()
        except BodyError:
            self.close_connection = True
        self.send_response(204)
        self.send_header("Allow", ALLOWED_METHODS)
        self.send_header("Content-Length", "0")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.config.log_requests:
            self._log_request_row("OPTIONS", path, 204, 0, 0)

    _head = False

    def _handle(self, method: str) -> None:
        started = time.time()
        path, query = _split(self.path)
        self._head = method == "HEAD"
        route_method = "GET" if self._head else method
        status = 500
        written = 0
        try:
            body = self._read_body()
        except BodyError as error:
            self.close_connection = True
            status, written = self._text(error.status, str(error))
            if self.config.log_requests:
                self._log_request_row(method, path, status, 0, written)
            return
        try:
            if self.config.latency_ms:
                time.sleep(self.config.latency_ms / 1000.0)
            if not self._authorised():
                status, written = self._challenge()
            elif (self.config.error_rate
                  and not path.startswith("/_mock")
                  and self.mock.random.random() < self.config.error_rate):
                status, written = self._text(
                    500, "injected failure (--error-rate %s)" % self.config.error_rate)
            else:
                with self.mock.lock:
                    status, written = self._route(route_method, path, query, body)
        except BrokenPipeError:          # pragma: no cover - client hung up
            return
        except BadQuery as error:
            status, written = self._json(400, {"error": str(error),
                                               "parameter": error.parameter})
        except Exception as error:       # pragma: no cover - last resort
            # A bug, then, and the one place it can be seen: the access log
            # has only the status line, so the traceback goes to stderr.
            traceback.print_exc()
            # Whatever the handler wrote before it failed is undone here, so
            # the request log's commit below cannot commit half of it.
            try:
                with self.mock.lock:
                    self.mock.conn.rollback()
            except sqlite3.Error:        # the database is already closed
                pass
            status, written = self._json(500, {"error": str(error),
                                               "type": type(error).__name__})
        finally:
            if self.config.log_requests:
                self._log_request_row(method, path, status, len(body or b""), written)

    def _route(self, method: str, path: str, query: Dict[str, List[str]],
               body: bytes) -> Tuple[int, int]:
        if path in ("/as2", "/as2/", "/as2/receive"):
            if method != "POST":
                return self._text(405, "POST an interchange here")
            return self._as2_inbound(body)
        if path in ("/as2/mdn", "/as2/mdn/"):
            if method != "POST":
                return self._text(405, "POST an MDN here")
            return self._as2_mdn(body)
        if path in ("/edi", "/edi/"):
            if method != "POST":
                return self._text(405, "POST an interchange here")
            return self._plain_inbound(body, query)
        if path.startswith("/_mock"):
            return self._control(method, path, query, body)
        if path in ("/", "/index.html"):
            return self._html(200, _index_page(self.mock, self._base()))
        return self._json(404, {"error": "no route for %s %s" % (method, path),
                                "try": ["/as2", "/edi", "/_mock/health", "/"]})

    # -- inbound

    def _as2_inbound(self, body: bytes) -> Tuple[int, int]:
        inbound = as2.read(self.headers)
        if not inbound.receiver and not inbound.sender:
            return self._text(400, "this endpoint expects AS2 headers; POST to "
                                   "/edi for a plain interchange")

        if inbound.secured:
            # Said plainly rather than mangled: see the note in as2.py.
            return self._mdn(inbound, body, as2.UNSUPPORTED,
                             "This mock does not implement S/MIME. Send the "
                             "payload unsigned and unencrypted, or use an AS2 "
                             "gateway in front of it.")
        if (self.config.strict_receiver and inbound.receiver
                and inbound.receiver != self.config.as2_id):
            return self._mdn(inbound, body, as2.ERROR,
                             "AS2-To is %r; this mock answers to %r."
                             % (inbound.receiver, self.config.as2_id))
        if inbound.wants_signed_receipt:
            # RFC 4130: a receiver that cannot produce the signed receipt the
            # sender required answers with a failure, not with an unsigned
            # success the sender has already said it will not accept. The
            # interchange is not read, for the same reason a refused envelope
            # is not: the sender has to send it again knowing the terms.
            return self._mdn(inbound, body, as2.FAILED,
                             "This mock does not sign: it has no S/MIME and no "
                             "certificate, and a signature it cannot produce is "
                             "not one it will pretend to. Ask for "
                             "signed-receipt-protocol=optional, or put a real "
                             "AS2 gateway in front of it.")

        mic = as2.mic(body, inbound.micalg) if body else ""
        receipts = self.mock.pipeline.receive(
            body, transport="as2", message_id=inbound.message_id, mic=mic,
            charset=charsets.from_content_type(self.headers.get("Content-Type", "")))
        if not any(receipt.ok for receipt in receipts):
            return self._mdn(inbound, body, as2.ERROR, receipts[0].error)

        # One MDN answers the whole payload, so it is only "processed" when
        # every interchange in it was: a file half of which was refused has
        # not been processed, whatever the other half did.
        explanation = _delivery_text(receipts)
        disposition = (as2.PROCESSED if all(r.accepted for r in receipts)
                       else as2.ERROR)
        return self._mdn(inbound, body, disposition, explanation)

    def _mdn(self, inbound: as2.Inbound, body: bytes, disposition: str,
             explanation: str) -> Tuple[int, int]:
        """Answer an AS2 POST: an MDN now, an MDN later, or neither."""
        if not self.config.mdn or not inbound.wants_mdn:
            return self._text(200, explanation)

        headers, payload = as2.build_mdn(
            inbound, body, self.config.as2_id, disposition, explanation,
            user_agent="mock-edi", moment=self.mock.pipeline.now())
        partner = inbound.sender or "unknown"

        if inbound.asynchronous:
            cursor = self.mock.conn.execute(
                "INSERT INTO mdn (partner, direction, original_id, message_id,"
                " disposition, mic, mode, url, status, payload, headers, at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (partner, "out", inbound.message_id, headers["Message-ID"],
                 disposition, as2.mic(body, inbound.micalg) if body else "",
                 "async", inbound.async_url, "pending",
                 payload.decode("utf-8", "replace"),
                 json.dumps(headers), db.now()))
            self.mock.conn.commit()
            self.mock.courier.enqueue_mdn(int(cursor.lastrowid))
            return self._text(202, "MDN will be posted to %s" % inbound.async_url)

        self.mock.conn.execute(
            "INSERT INTO mdn (partner, direction, original_id, message_id,"
            " disposition, mic, mode, status, payload, at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (partner, "out", inbound.message_id, headers["Message-ID"],
             disposition, as2.mic(body, inbound.micalg) if body else "",
             "sync", "sent", payload.decode("utf-8", "replace"), db.now()))
        self.mock.conn.commit()
        return self._raw(200, payload, headers)

    def _as2_mdn(self, body: bytes) -> Tuple[int, int]:
        """A partner acknowledging something the mock sent."""
        inbound = as2.read(self.headers)
        fields = as2.parse_mdn(body)
        self.mock.conn.execute(
            "INSERT INTO mdn (partner, direction, original_id, message_id,"
            " disposition, mic, mode, status, payload, at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (inbound.sender or "unknown", "in",
             fields.get("Original-Message-ID", ""), inbound.message_id,
             fields.get("Disposition", ""), fields.get("Received-Content-MIC", ""),
             "async", "received", body.decode("utf-8", "replace"), db.now()))
        self.mock.conn.commit()
        return self._json(200, {"recorded": True, "fields": fields})

    def _plain_inbound(self, body: bytes, query) -> Tuple[int, int]:
        receipts = self.mock.pipeline.receive(
            body, transport="http",
            charset=charsets.from_content_type(self.headers.get("Content-Type", "")))
        if len(receipts) == 1 and not receipts[0].ok:
            return self._json(422, {"accepted": False, "error": receipts[0].error})

        # A payload holding several interchanges answers with the same keys it
        # always has, summed over the lot, and `interchanges` holds them one by
        # one. For the usual payload of one that is the same object twice, so
        # nothing reading this needs to know about the case until it meets it.
        each = [_interchange_summary(receipt) for receipt in receipts]
        summary = dict(each[0])
        summary.update({
            "accepted": all(item["accepted"] for item in each),
            "orders": [po for item in each for po in item["orders"]],
            "transactionSets": [t for item in each for t in item["transactionSets"]],
            "queued": [q for item in each for q in item["queued"]],
            "acknowledged": [a for item in each for a in item["acknowledged"]],
            "changed": [c for item in each for c in item["changed"]],
            "refusals": [r for item in each for r in item["refusals"]],
            "interchanges": each,
        })
        return self._json(200, summary)

    # -- the control plane

    def _control(self, method: str, path: str, query: Dict[str, List[str]],
                 body: bytes) -> Tuple[int, int]:
        parts = _segments(path)[1:]                     # drop "_mock"
        head = parts[0] if parts else ""
        rest = parts[1:]
        conn = self.mock.conn

        if head == "health":
            return self._json(200, {
                "status": "ok", "as2Id": self.config.as2_id,
                "started": self.mock.started.isoformat(),
                "partners": _count(conn, "partner"),
                "queued": _count(conn, "outbound", "status = 'ready'"),
            })

        if head == "state":
            return self._json(200, {
                "as2Id": self.config.as2_id,
                "database": self.config.db_path,
                "counts": {name: _count(conn, name) for name in (
                    "partner", "catalog", "interchange", "transaction_set",
                    "purchase_order", "order_line", "shipment", "invoice",
                    "outbound", "scheduled", "mdn")},
                "scheduled": {
                    "waiting": _count(conn, "scheduled", "done_at = ''"),
                    "done": _count(conn, "scheduled", "done_at != ''")},
                "queue": {status: _count(conn, "outbound", "status = '%s'" % status)
                          for status in ("pending", "ready", "delivered",
                                         "collected", "failed")},
                "delays": {
                    "acknowledgment": self.config.ack_delay_ms,
                    "response": self.config.response_delay_ms,
                    "despatch": self.config.despatch_delay_ms,
                    "invoice": self.config.invoice_delay_ms},
                "courierFailures": list(self.mock.courier.failures[-10:]),
            })

        if head == "behaviours":
            return self._json(200, partners.BEHAVIOURS)

        if head == "dictionary":
            return self._json(200, _dictionary(rest))

        if head == "partners":
            return self._partners(method, rest, query, body)

        if head == "catalog":
            return self._json(200, db.rows(conn, "SELECT * FROM catalog ORDER BY sku"))

        if head == "orders":
            if rest:
                order = documents.order_row(conn, rest[0])
                if order is None:
                    return self._json(404, {"error": "no purchase order %r" % rest[0]})
                order["lines"] = documents.order_lines(conn, order["po_number"])
                order["shipments"] = db.rows(
                    conn, "SELECT * FROM shipment WHERE po_number = ? ORDER BY rowid",
                    (order["po_number"],))
                order["invoices"] = db.rows(
                    conn, "SELECT * FROM invoice WHERE po_number = ? ORDER BY rowid",
                    (order["po_number"],))
                return self._json(200, order)
            return self._json(200, db.rows(
                conn, "SELECT * FROM purchase_order ORDER BY rowid DESC LIMIT ?",
                (_limit(query),)))

        if head == "documents":
            if rest:
                row = db.one(conn, "SELECT * FROM transaction_set WHERE id = ?",
                             (rest[0],))
                if row is None:
                    return self._json(404, {"error": "no document %r" % rest[0]})
                interchange = db.one(conn, "SELECT * FROM interchange WHERE id = ?",
                                     (row["interchange_id"],))
                row["findings"] = json.loads(row["findings"] or "[]")
                row["payload"] = interchange["payload"] if interchange else ""
                return self._json(200, row)
            return self._json(200, [
                dict(row, findings=json.loads(row["findings"] or "[]"))
                for row in db.rows(conn, _document_query(query), _document_params(query))])

        if head == "interchanges":
            if rest:
                row = db.one(conn, "SELECT * FROM interchange WHERE id = ?", (rest[0],))
                if row is None:
                    return self._json(404, {"error": "no interchange %r" % rest[0]})
                if _flag(query, "raw"):
                    # The bytes as they arrived or left; a row from before
                    # they were kept has only its text.
                    raw = row["raw"] if row["raw"] is not None \
                        else row["payload"].encode("utf-8")
                    content_type = _edi_type(row["dialect"])
                    if row["charset"]:
                        content_type += "; charset=%s" % row["charset"]
                    return self._raw(200, bytes(raw), {"Content-Type": content_type})
                row = dict(row)
                row.pop("raw", None)
                return self._json(200, row)
            return self._json(200, db.rows(
                conn, "SELECT id, direction, dialect, partner, control, transport,"
                      " message_id, mic, at, length(payload) AS bytes FROM interchange"
                      " ORDER BY id DESC LIMIT ?", (_limit(query),)))

        if head == "mailbox":
            rows = self.mock.pipeline.collect(
                partner_id=_first(query, "partner"), kind=_first(query, "kind"),
                leave=_flag(query, "leave"))
            if _flag(query, "raw"):
                joined = b"\n".join(self.mock.pipeline.wire(row)[0] for row in rows)
                return self._raw(200, joined, {"Content-Type": TEXT})
            return self._json(200, rows)

        if head == "outbox":
            if len(rest) == 2 and rest[1] == "retry":
                if method != "POST":
                    return self._text(405, "POST to retry a delivery")
                try:
                    outbound_id = int(rest[0])
                except ValueError:
                    return self._json(404, {"error": "no outbound document %r"
                                                     % rest[0]})
                row = db.one(conn, "SELECT status FROM outbound WHERE id = ?",
                             (outbound_id,))
                if row is None:
                    return self._json(404, {"error": "no outbound document %d"
                                                     % outbound_id})
                if row["status"] != "failed":
                    return self._json(409, {
                        "error": "outbound document %d is %s, not failed; only a "
                                 "failed delivery can be retried"
                                 % (outbound_id, row["status"])})
                retried = self.mock.pipeline.redeliver(outbound_id)
                return self._json(200, {"retried": retried, "count": len(retried)})
            return self._json(200, db.rows(
                conn, "SELECT id, partner, dialect, code, kind, reference, status,"
                      " message_id, control, due_at, released_at, delivered_at,"
                      " delivery, note, attempts, last_error, last_attempt_at,"
                      " at FROM outbound ORDER BY id DESC LIMIT ?",
                (_limit(query),)))

        if head == "drop":
            if rest and rest[0] == "scan":
                if method != "POST":
                    return self._text(405, "POST to scan the drop directory")
                if not self.mock.dropbox.drop_dir:
                    return self._json(409, {
                        "error": "no drop directory is configured; start the "
                                 "mock with --drop-dir"})
                found = self.mock.dropbox.scan()
                return self._json(200, {"scanned": len(found),
                                        "files": [vars(item) for item in found]})
            return self._json(200, self.mock.dropbox.state())

        if head == "scheduled":
            # Work the seller has promised but not done: the despatch that is
            # not packed yet, the invoice that is not written yet. Distinct
            # from the outbox, which holds documents that already exist.
            clause = "" if _flag(query, "all") else " WHERE done_at = ''"
            return self._json(200, db.rows(
                conn, "SELECT id, partner, po_number, kind, due_at, done_at,"
                      " note, at FROM scheduled%s ORDER BY due_at, id LIMIT ?"
                      % clause, (_limit(query),)))

        if head == "advance":
            if method != "POST":
                return self._text(405, "POST to advance the queue")
            if _flag(query, "failed"):
                # Everything a partner's listener missed while it was down,
                # in queue order, unchanged.
                retried = self.mock.pipeline.redeliver(
                    partner_id=_first(query, "partner") or "")
                return self._json(200, {"retried": retried,
                                        "count": len(retried)})
            everything = _flag(query, "all")
            seconds = _number(query, "seconds")
            try:
                released = self.mock.pipeline.advance(seconds, everything)
            except ValueError as error:
                return self._json(400, {"error": str(error), "parameter": "seconds"})
            clock = self.mock.pipeline
            return self._json(200, {
                "released": released, "count": len(released),
                "clock": clock.now().isoformat(timespec="seconds"),
                "advancedSeconds": clock.offset.total_seconds()})

        if head == "send":
            if method != "POST":
                return self._text(405, "POST to send a document")
            payload = _json_body(body)
            try:
                queued = self.mock.pipeline.send_document(
                    payload.get("partner", ""), payload.get("kind", ""),
                    payload.get("order", "") or payload.get("po", ""),
                    int(payload.get("delayMs", 0)))
            except partners.UnknownPartner as error:
                return self._json(404, {"error": "no partner %s" % error})
            except ValueError as error:
                return self._json(400, {"error": str(error)})
            return self._json(201, {"id": queued.id, "kind": queued.kind,
                                    "code": queued.code, "dueAt": queued.due_at})

        if head == "unacknowledged":
            return self._json(200, reconcile.unacknowledged(
                conn, _number(query, "older-than"),
                _first(query, "partner"), _limit(query)))

        if head == "mdns":
            return self._json(200, db.rows(
                conn, "SELECT id, partner, direction, original_id, message_id,"
                      " disposition, mic, mode, url, status, at FROM mdn"
                      " ORDER BY id DESC LIMIT ?", (_limit(query),)))

        if head == "requests":
            return self._json(200, db.rows(
                conn, "SELECT * FROM request_log ORDER BY id DESC LIMIT ?",
                (_limit(query),)))

        if head == "validate":
            if method != "POST":
                return self._text(405, "POST an interchange to validate it")
            return self._validate_only(body)

        if head == "reset":
            if method != "POST":
                return self._text(405, "POST to reset")
            self.mock.reset()
            return self._json(200, {"reset": True})

        return self._json(404, {
            "error": "no control endpoint %r" % head,
            "endpoints": ["health", "state", "behaviours", "dictionary", "partners",
                          "catalog", "orders", "documents", "interchanges",
                          "mailbox", "outbox", "scheduled", "drop", "advance", "send", "mdns",
                          "unacknowledged", "requests", "validate", "reset"]})

    def _partners(self, method: str, rest: List[str], query, body: bytes):
        conn = self.mock.conn
        if not rest:
            if method == "GET":
                return self._json(200, partners.listing(conn))
            if method == "POST":
                payload = _json_body(body)
                identifier = payload.pop("id", "")
                if not identifier:
                    return self._json(400, {"error": "a partner needs an id"})
                try:
                    row = partners.create(conn, identifier, payload.pop("name", ""),
                                          **payload)
                except ValueError as error:
                    return self._json(400, {"error": str(error)})
                return self._json(201, row)
            return self._text(405, "GET or POST partners")

        identifier = rest[0]
        if method == "GET":
            row = partners.get(conn, identifier)
            if row is None:
                return self._json(404, {"error": "no partner %r" % identifier})
            return self._json(200, row)
        if method in ("PATCH", "PUT"):
            try:
                row = partners.update(conn, identifier, **_json_body(body))
            except partners.UnknownPartner:
                return self._json(404, {"error": "no partner %r" % identifier})
            except ValueError as error:
                return self._json(400, {"error": str(error)})
            return self._json(200, row)
        if method == "DELETE":
            outcome = partners.delete(conn, identifier)
            return self._json(200 if outcome["deleted"] else 404, outcome)
        return self._text(405, "GET, PATCH or DELETE a partner")

    def _validate_only(self, body: bytes) -> Tuple[int, int]:
        """Check an interchange and say what is wrong, changing nothing.

        The mock's validator, without the trading partner attached: useful
        while writing a mapping, when what you want is the findings and not
        four documents in a mailbox.
        """
        from . import edifact, x12
        from .envelope import sniff
        http_charset = charsets.from_content_type(self.headers.get("Content-Type", ""))
        view = body.decode(charsets.BYTES)
        try:
            dialect = sniff(view)
            parts = x12.split(view) if dialect == "X12" else edifact.split(view)
            texts = []
            for part in parts:
                raw = part.encode(charsets.BYTES)
                texts.append(charsets.decode(
                    raw, charsets.declared(dialect, raw, http_charset)))
            interchanges = [x12.parse(text) if dialect == "X12"
                            else edifact.parse(text) for text in texts]
        except EdiSyntaxError as error:
            return self._json(422, {"parsed": False, "error": str(error)})
        reports = [validate.validate(item) for item in interchanges]
        interchange, report = interchanges[0], reports[0]
        from . import ack
        return self._json(200, {
            "parsed": True,
            "dialect": dialect,
            "sender": interchange.sender,
            "receiver": interchange.receiver,
            "control": interchange.control,
            "clean": all(item.clean for item in reports),
            "groupCode": report.group_code,
            # As with the plain endpoint: the keys beside this one describe the
            # first interchange, and this describes each of them.
            "interchanges": [
                {"control": item.control, "sender": item.sender,
                 "receiver": item.receiver, "clean": each.clean,
                 "explain": ack.explain(each)}
                for item, each in zip(interchanges, reports)],
            "messages": [
                {"code": m.code, "control": m.control, "kind": m.kind,
                 "accepted": m.accepted, "findings": _findings(m)}
                for m in report.messages],
            "explain": ack.explain(report),
        })

    # -- responses

    def _read_body(self) -> bytes:
        """The request body, from Content-Length or chunked transfer coding.

        Refused rather than guessed at: a length that is not a number, a
        body over `max_body_bytes`, a transfer coding other than chunked, and
        a client that stops sending all raise `BodyError`.
        """
        coding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if coding and coding != "identity":
            if coding != "chunked":
                raise BodyError(501, "Transfer-Encoding %r is not supported; "
                                     "send chunked or a Content-Length" % coding)
            return self._read_chunked()
        length = (self.headers.get("Content-Length") or "").strip()
        if not length:
            return b""
        if not length.isdigit():
            raise BodyError(400, "Content-Length must be a non-negative number, "
                                 "not %r" % length)
        size = int(length)
        if size > self.config.max_body_bytes:
            raise BodyError(413, "the body is %d bytes; this mock reads at most "
                                 "%d (--max-body)" % (size, self.config.max_body_bytes))
        body = self._read_exactly(size)
        return body

    def _read_exactly(self, size: int) -> bytes:
        try:
            body = self.rfile.read(size)
        except (TimeoutError, OSError):
            raise BodyError(408, "the body did not arrive within %gs"
                                 % self.config.request_timeout)
        if len(body) < size:
            raise BodyError(400, "the body ended after %d of %d bytes"
                                 % (len(body), size))
        return body

    def _read_line(self) -> bytes:
        try:
            return self.rfile.readline(65537)
        except (TimeoutError, OSError):
            raise BodyError(408, "the body did not arrive within %gs"
                                 % self.config.request_timeout)

    def _read_chunked(self) -> bytes:
        """RFC 9112 chunked coding: hex sizes, data, CRLF, a zero, trailers."""
        chunks: List[bytes] = []
        total = 0
        while True:
            line = self._read_line()
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise BodyError(400, "a chunk size must be hexadecimal, not %r"
                                     % line[:40])
            if size < 0:
                raise BodyError(400, "a chunk size cannot be negative")
            if size == 0:
                # Trailer fields, if any, end with an empty line.
                while self._read_line() not in (b"\r\n", b"\n", b""):
                    pass
                return b"".join(chunks)
            total += size
            if total > self.config.max_body_bytes:
                raise BodyError(413, "the body is over %d bytes (--max-body)"
                                     % self.config.max_body_bytes)
            chunks.append(self._read_exactly(size))
            if self._read_line() not in (b"\r\n", b"\n"):
                raise BodyError(400, "a chunk is not followed by CRLF")

    def _raw(self, status: int, payload: bytes,
             headers: Optional[Dict[str, str]] = None) -> Tuple[int, int]:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if not any(k.lower() == "content-type" for k in (headers or {})):
            self.send_header("Content-Type", TEXT)
        self.send_header("Content-Length", str(len(payload)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self._head:
            return status, 0
        self.wfile.write(payload)
        return status, len(payload)

    def _json(self, status: int, payload: Any) -> Tuple[int, int]:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        return self._raw(status, body, {"Content-Type": JSON})

    def _text(self, status: int, message: str) -> Tuple[int, int]:
        return self._raw(status, (message + "\n").encode("utf-8"),
                         {"Content-Type": TEXT})

    def _html(self, status: int, markup: str) -> Tuple[int, int]:
        return self._raw(status, markup.encode("utf-8"), {"Content-Type": HTML})

    # -- authentication

    def _authorised(self) -> bool:
        if not self.config.basic_auth:
            return True
        import base64
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
        except Exception:
            return False
        return decoded == self.config.basic_auth

    def _challenge(self) -> Tuple[int, int]:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="mock-edi"')
        self.send_header("Content-Type", TEXT)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return 401, 0

    def _base(self) -> str:
        host = self.headers.get("Host") or "%s:%d" % (self.config.host, self.config.port)
        return "http://%s" % host

    def _log_request_row(self, method: str, path: str, status: int,
                         bytes_in: int, bytes_out: int) -> None:
        """Record the request - under the lock, like every other write.

        This was the one database write outside it, on the grounds that
        logging is harmless. It is not: `commit()` commits the *connection*,
        not the statement, so a log entry written while another thread was
        mid-transaction committed that thread's work early and left its own
        `commit()` with nothing to do. sqlite3 answers that with "cannot
        commit - no transaction is active", and the request that had done the
        real work failed with a 500.
        """
        try:
            with self.mock.lock:
                self.mock.conn.execute(
                    "INSERT INTO request_log (method, path, status, bytes_in,"
                    " bytes_out, partner, at) VALUES (?,?,?,?,?,?,?)",
                    (method, path, status, bytes_in, bytes_out,
                     self.headers.get("AS2-From", "") or "", db.now()))
                self.mock.conn.commit()
        except sqlite3.Error:            # pragma: no cover - logging must not fail a request
            pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _split(target: str) -> Tuple[str, Dict[str, List[str]]]:
    parsed = urllib.parse.urlsplit(target)
    # The path stays encoded: it is split on `/` before any segment is
    # decoded, or an encoded slash in a PO number (`PO%2F2026%2F1`) would
    # become a real one and the order could never be reached. `_segments`
    # decodes each piece, once.
    # `keep_blank_values` matters: the flags are written `?all`, `?raw`,
    # `?leave`, with no value at all, and the default parse drops them - so
    # every flag silently read as false.
    return (parsed.path,
            urllib.parse.parse_qs(parsed.query, keep_blank_values=True))


def _segments(path: str) -> List[str]:
    """The segments of a still-encoded path, each percent-decoded once."""
    return [urllib.parse.unquote(p) for p in path.split("/") if p]


def _first(query: Dict[str, List[str]], name: str, default: str = "") -> str:
    values = query.get(name) or []
    return values[0] if values else default


def _flag(query: Dict[str, List[str]], name: str) -> bool:
    value = _first(query, name, "").lower()
    return value in ("", "1", "true", "yes") and name in query


class BadQuery(ValueError):
    """A query-string value that cannot be read; answered 400, naming it."""

    def __init__(self, parameter: str, message: str):
        super().__init__(message)
        self.parameter = parameter


def _number(query: Dict[str, List[str]], name: str, default: float = 0.0) -> float:
    raw = _first(query, name)
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value):
        raise BadQuery(name, "%s must be a number, got %r" % (name, raw))
    return value


def _limit(query: Dict[str, List[str]], default: int = 50) -> int:
    raw = _first(query, "limit")
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadQuery("limit", "limit must be a whole number, got %r" % raw) from None
    return max(1, min(1000, value))


def _count(conn: sqlite3.Connection, table: str, where: str = "") -> int:
    sql = "SELECT COUNT(*) AS n FROM %s%s" % (table, " WHERE " + where if where else "")
    return int(conn.execute(sql).fetchone()["n"])


def _json_body(body: bytes) -> Dict[str, Any]:
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _edi_type(dialect: str) -> str:
    return "application/edi-x12" if dialect == "X12" else "application/edifact"


def _findings(message_report) -> List[str]:
    out: List[str] = []
    for finding in message_report.segments:
        for element in finding.elements:
            out.append("%s@%d: %s" % (finding.tag, finding.position, element.note))
        if not finding.elements:
            out.append("%s@%d: %s" % (finding.tag, finding.position, finding.note))
    out.extend(note for _code, note in message_report.set_errors)
    return out


def _interchange_summary(receipt) -> Dict[str, Any]:
    """One interchange's outcome, as the plain endpoint reports it."""
    report = receipt.report
    summary = {
        "accepted": receipt.accepted,
        "partner": receipt.partner,
        "dialect": receipt.dialect,
        "interchange": receipt.control,
        "orders": receipt.orders,
        "transactionSets": [
            {"code": m.code, "control": m.control, "kind": m.kind,
             "accepted": m.accepted, "findings": _findings(m)}
            for m in (report.messages if report else [])],
        "queued": [{"kind": q.kind, "code": q.code, "reference": q.reference,
                    "dueAt": q.due_at} for q in receipt.queued],
        "acknowledged": receipt.acknowledged,
        "changed": receipt.changes,
        "refusals": receipt.refusals,
    }
    if not receipt.ok:
        # One interchange of several can be refused while the rest are read.
        summary["error"] = receipt.error
    return summary


def _delivery_text(receipts) -> str:
    """The prose an MDN carries about a whole payload."""
    if len(receipts) == 1:
        return _receipt_text(receipts[0])
    parts = ["The payload held %d interchanges." % len(receipts)]
    parts.extend(receipt.error or _receipt_text(receipt) for receipt in receipts)
    return " ".join(parts)


def _receipt_text(receipt) -> str:
    """The prose an MDN carries about what the mock made of the interchange."""
    report = receipt.report
    if report is None:
        return "The interchange was received."
    parts = ["Interchange %s from %s: %d transaction set(s), %d accepted."
             % (receipt.control, receipt.partner, report.received, report.accepted)]
    for message in report.messages:
        if not message.clean:
            parts.append("%s/%s: %s" % (message.code, message.control,
                                        message.summary()))
    if receipt.orders:
        parts.append("Purchase order(s) recorded: %s." % ", ".join(receipt.orders))
    if receipt.queued:
        parts.append("Queued in reply: %s."
                     % ", ".join(q.code for q in receipt.queued))
    return " ".join(parts)


def _document_query(query: Dict[str, List[str]]) -> str:
    clauses = []
    if "acknowledged" in query:
        # `?acknowledged=false` is the useful one: what have we sent that
        # nobody has answered for?
        clauses.append("ack_status %s ''"
                       % ("!=" if _flag(query, "acknowledged") else "="))
    if _first(query, "direction"):
        clauses.append("direction = ?")
    if _first(query, "partner"):
        clauses.append("partner = ?")
    if _first(query, "kind"):
        clauses.append("kind = ?")
    if _first(query, "code"):
        clauses.append("code = ?")
    if _first(query, "reference"):
        clauses.append("reference = ?")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return ("SELECT * FROM transaction_set%s ORDER BY id DESC LIMIT %d"
            % (where, _limit(query)))


def _document_params(query: Dict[str, List[str]]) -> List[str]:
    return [_first(query, name) for name in
            ("direction", "partner", "kind", "code", "reference")
            if _first(query, name)]


def _dictionary(rest: List[str]) -> Any:
    """The dictionary, served as data.

    Everything the mock validates against is derived from `schema.py`, so
    publishing it is not documentation that can go stale - it is the rules
    themselves.  A mapping tool can read this instead of a PDF.
    """
    if not rest:
        return {
            "dialects": list(schema.DIALECTS),
            "transactionSets": [
                {"dialect": item.dialect, "code": item.code, "name": item.name,
                 "kind": schema.kind_of(item.dialect, item.code),
                 "group": item.group, "version": item.version,
                 "purpose": item.purpose,
                 "segments": list(item.known_tags())}
                for item in schema.SETS.values()],
        }
    dialect = rest[0].upper()
    if len(rest) == 1:
        return {"dialect": dialect,
                "transactionSets": sorted(code for d, code in schema.SETS
                                          if d == dialect)}
    definition = schema.lookup(dialect, rest[1].upper())
    if definition is None:
        return {"error": "no transaction set %s/%s" % (dialect, rest[1])}
    return {
        "dialect": definition.dialect, "code": definition.code,
        "name": definition.name, "purpose": definition.purpose,
        "group": definition.group, "version": definition.version,
        "segments": [
            {"tag": use.tag, "name": use.segment.name, "requirement": use.req,
             "maxUse": use.max_use, "loop": loop.id if loop else "",
             "purpose": use.segment.purpose,
             # `width` is how wide the standard makes the segment; the
             # elements below are the ones this mock checks. Where they differ,
             # the positions in between are carried and not validated - so a
             # guide that uses one of them is not wrong, it is untested.
             "width": use.segment.width,
             "checkedTo": len(use.segment.elements),
             "elements": [
                 {"position": position, "ref": element.ref, "name": element.name,
                  "type": element.type, "requirement": element.req,
                  "length": "%d/%d" % (element.min_len, element.max_len),
                  "codes": sorted(element.codes) if element.codes else None,
                  "components": [
                      {"ref": c.ref, "name": c.name, "requirement": c.req,
                       "codes": sorted(c.codes) if c.codes else None}
                      for c in element.components] or None}
                 for position, element in enumerate(use.segment.elements, start=1)]}
            for use, loop in definition.uses()],
    }


# ---------------------------------------------------------------------------
# The index page
# ---------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; --fg:#1a1a1a; --bg:#fbfbfa; --muted:#6b6b6b;
        --line:#e3e3e0; --accent:#8b5a2b; --code:#f1f0ee; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e6e3; --bg:#1c1c1a; --muted:#9a9894; --line:#33322f;
          --accent:#d4a373; --code:#262523; } }
* { box-sizing: border-box; }
body { font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
       color: var(--fg); background: var(--bg); margin: 0; padding: 32px 16px; }
main { max-width: 860px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 32px 0 8px; border-bottom: 1px solid var(--line);
     padding-bottom: 6px; }
p.lead { color: var(--muted); margin: 0 0 8px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
code, a code { background: var(--code); padding: 1px 5px; border-radius: 4px;
       font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.tag { font-size: 12px; color: var(--muted); }
"""


def _index_page(mock: Mock, base: str) -> str:
    conn = mock.conn
    rows = partners.listing(conn)
    partner_rows = "".join(
        "<tr><td><code>%s</code></td><td>%s</td><td>%s</td>"
        "<td><code>%s</code></td><td>%s</td></tr>"
        % (row["id"], _esc(row["name"]), row["dialect"], row["behaviour"],
           _esc(partners.BEHAVIOURS.get(row["behaviour"], "")))
        for row in rows)

    endpoints = [
        ("POST", "/as2", "An AS2 interchange. Answers with an MDN."),
        ("POST", "/edi", "The same, without AS2. Answers with a JSON summary."),
        ("POST", "/as2/mdn", "An asynchronous MDN coming back to us."),
        ("POST", "/_mock/validate", "Check a document, change nothing."),
        ("GET", "/_mock/health", "Liveness."),
        ("GET", "/_mock/state", "Counts, queue depth, configured delays."),
        ("GET", "/_mock/partners", "Who we trade with, and how each misbehaves."),
        ("GET", "/_mock/catalog", "What we sell."),
        ("GET", "/_mock/orders", "Purchase orders received, and what became of them."),
        ("GET", "/_mock/documents", "Every transaction set, in and out."),
        ("GET", "/_mock/interchanges", "Raw payloads. Add <code>?raw</code> for one."),
        ("GET", "/_mock/mailbox", "Collect what is waiting. <code>?leave</code> to peek."),
        ("GET", "/_mock/outbox", "Documents produced, and what became of them."),
        ("GET", "/_mock/scheduled", "Work promised but not done: the unpacked despatch, the unwritten invoice."),
        ("GET", "/_mock/drop", "The drop and pickup directories, and what they have seen."),
        ("POST", "/_mock/drop/scan", "Read the drop directory now, without waiting for a poll."),
        ("POST", "/_mock/advance", "Release what is due. <code>?all</code> for everything, "
                                   "<code>?failed</code> to redeliver what failed."),
        ("POST", "/_mock/outbox/{id}/retry", "Deliver a failed document again, unchanged."),
        ("POST", "/_mock/send", "Send a document out of band."),
        ("GET", "/_mock/mdns", "Receipts, sent and received."),
        ("GET", "/_mock/unacknowledged", "What we sent that nobody has acknowledged."),
        ("GET", "/_mock/dictionary", "The segment dictionary the mock validates against."),
        ("POST", "/_mock/reset", "Back to a freshly seeded system."),
    ]
    endpoint_rows = "".join(
        '<tr><td class="tag">%s</td><td><a href="%s">%s</a></td><td>%s</td></tr>'
        % (method, path if method == "GET" else "#", path, note)
        for method, path, note in endpoints)

    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mock-edi</title><style>%s</style></head><body><main>
<h1>mock-edi</h1>
<p class="lead">A mock EDI trading partner answering as <code>%s</code>.
Send it an 850 or an ORDERS and it answers with an acknowledgment, a purchase
order response, a despatch advice and an invoice.</p>
<h2>Trading partners</h2>
<table><tr><th>Id</th><th>Name</th><th>Dialect</th><th>Behaviour</th><th></th></tr>%s</table>
<h2>Endpoints</h2>
<table><tr><th></th><th>Path</th><th></th></tr>%s</table>
<h2>Try it</h2>
<p class="lead">There is a worked example in <code>examples/demo.sh</code>.
The short version:</p>
<p><code>curl -X POST --data-binary @order.edi %s/edi</code></p>
</main></body></html>""" % (_STYLE, mock.config.as2_id, partner_rows,
                            endpoint_rows, base)


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class _Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    mock: Mock

    def server_close(self):
        # `socketserver` calls this from its own constructor when binding
        # fails, before `make_server` has attached the mock - so asking for
        # `self.mock` here would replace "address already in use" with an
        # AttributeError and hide the actual problem.
        mock = getattr(self, "mock", None)
        try:
            if mock is not None:
                mock.close()
        finally:
            HTTPServer.server_close(self)


def make_server(config: Config) -> _Server:
    """Build a server. It is not listening until `serve_forever` is called."""
    httpd = _Server((config.host, config.port), Handler)
    try:
        httpd.mock = Mock(config)
    except BaseException:
        # A --db file that cannot be used: let go of the port too.
        httpd.server_close()
        raise
    if httpd.mock.dropbox.active:
        httpd.mock.dropbox.prepare()
        httpd.mock.dropbox.start()
    httpd.mock.resume()
    return httpd
