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

Which function answers which path is the route table's business, in
`mockedi.routes`; this module reads the request, authenticates it, holds the
lock while a route runs, and logs the answer.
"""
from __future__ import annotations

import datetime
import hmac
import json
import random
import socketserver
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

from . import db, delivery, drop, pipeline, routes
from .routes import BadQuery
from .routes import split as _split

JSON = "application/json; charset=utf-8"
TEXT = "text/plain; charset=utf-8"
HTML = "text/html; charset=utf-8"
ALLOWED_METHODS = "GET, HEAD, POST, PATCH, PUT, DELETE, OPTIONS"

# How many logged requests pass between prunes, so a mock nobody advances is
# still bounded without a timer of its own.
PRUNE_EVERY = 1000


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
    # Hosts the courier may POST to. Empty means anywhere, which is what a
    # laptop wants. A mock reachable from a network is a different matter: it
    # posts released documents to whatever `as2_url` a partner carries, and
    # asynchronous MDNs to whatever `Receipt-Delivery-Option` an *unauthenticated*
    # AS2 sender names - `/as2` cannot require authentication and still be AS2.
    # So it can be asked to post stored payloads at an internal address, and
    # this says which ones are allowed.
    deliver_to: Tuple[str, ...] = ()
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

    # Retention, for a mock left running on a file database. Pruned at
    # startup, after every advance and every PRUNE_EVERY requests.
    keep_requests: int = 5000        # newest request-log rows kept; 0 keeps all
    retention_days: float = 0.0      # older records removed; 0 keeps everything


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
        self.started = db.utcnow()
        self.random = random.Random(config.seed_value)
        self.pruned: Dict[str, int] = {}
        self.requests_since_prune = 0
        self.prune()

    def prune(self) -> Dict[str, int]:
        """Apply --keep-requests and --retention-days, and keep a running total."""
        with self.lock:
            removed = db.prune(self.conn, self.config.keep_requests,
                               self.config.retention_days)
            for table, count in removed.items():
                self.pruned[table] = self.pruned.get(table, 0) + count
            self.requests_since_prune = 0
        return removed

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
                          "request_log", "partner_profile", "supplier_claim",
                          "disagreement",
                          "partner", "catalog"):
                self.conn.execute("DELETE FROM %s" % table)
            self.conn.commit()
            db.seed(self.conn, self.config.seed_value, self.config.as2_id)
            self.pipeline.offset = datetime.timedelta(0)
            # What the courier and the dropbox hold in memory describes the
            # data just thrown away, so /_mock/state and /_mock/drop would
            # otherwise report failures and files for orders that are gone.
            self.courier.forget()
            self.dropbox.forget()
            self.pruned = {}


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
        self._begin_log("OPTIONS", path)
        try:
            # Read and discarded: on a kept-alive connection, a body left
            # unread would be taken for the next request.
            self._bytes_in = len(self._read_body())
        except BodyError:
            self.close_connection = True
        self._log_before_answering(204, 0)
        self.send_response(204)
        self.send_header("Allow", ALLOWED_METHODS)
        self.send_header("Content-Length", "0")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

    # What a route reads: the method it is answered as (GET for a HEAD), the
    # query and the body of the request being answered. They are the
    # handler's, which answers one request at a time on its own connection's
    # thread and sets them afresh for each; nothing on another thread - the
    # courier, the drop poller - may read them.
    method = ""
    query: Dict[str, List[str]] = {}
    body = b""
    _head = False

    def _handle(self, method: str) -> None:
        started = time.time()
        path, self.query = _split(self.path)
        self._head = method == "HEAD"
        self._begin_log(method, path)
        self.method = "GET" if self._head else method
        status = 500
        written = 0
        self.body = b""
        try:
            self.body = self._read_body()
        except BodyError as error:
            self.close_connection = True
            status, written = self.text(error.status, str(error))
            return
        self._bytes_in = len(self.body)
        try:
            if self.config.latency_ms:
                time.sleep(self.config.latency_ms / 1000.0)
            if not self._authorised():
                status, written = self._challenge()
            elif (self.config.error_rate
                  and not path.startswith("/_mock")
                  and self.mock.random.random() < self.config.error_rate):
                status, written = self.text(
                    500, "injected failure (--error-rate %s)" % self.config.error_rate)
            else:
                with self.mock.lock:
                    status, written = self._route(self.method, path)
        except BrokenPipeError:          # pragma: no cover - client hung up
            return
        except BadQuery as error:
            status, written = self.json(400, {"error": str(error),
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
            status, written = self.json(500, {"error": str(error),
                                               "type": type(error).__name__})
        finally:
            # Only a request that was never answered - the client hung up
            # first - reaches here unlogged; every answer is logged as it is
            # sent. See _log_before_answering.
            self._log_before_answering(status, written)

    def _route(self, method: str, path: str) -> Tuple[int, int]:
        found, arguments = routes.find(method, path)
        if found is None:
            return self.json(404, {"error": "no route for %s %s" % (method, path),
                                    "try": ["/as2", "/edi", "/_mock/health", "/"]})
        if found.method not in (routes.ANY, method):
            return self.text(405, found.refuse)
        return found.function(self, *arguments)

    # -- the control plane

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

    def raw(self, status: int, payload: bytes,
             headers: Optional[Dict[str, str]] = None) -> Tuple[int, int]:
        self._log_before_answering(status, 0 if self._head else len(payload))
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

    def json(self, status: int, payload: Any) -> Tuple[int, int]:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        return self.raw(status, body, {"Content-Type": JSON})

    def text(self, status: int, message: str) -> Tuple[int, int]:
        return self.raw(status, (message + "\n").encode("utf-8"),
                         {"Content-Type": TEXT})

    def html(self, status: int, markup: str) -> Tuple[int, int]:
        return self.raw(status, markup.encode("utf-8"), {"Content-Type": HTML})

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
        # Constant time, so that the comparison does not leak how much of the
        # credential was right. This is a mock and the stakes are low, but the
        # one-line version of the right answer costs nothing.
        return hmac.compare_digest(decoded, self.config.basic_auth)

    def _challenge(self) -> Tuple[int, int]:
        self._log_before_answering(401, 0)
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="mock-edi"')
        self.send_header("Content-Type", TEXT)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return 401, 0

    def base(self) -> str:
        host = self.headers.get("Host") or "%s:%d" % (self.config.host, self.config.port)
        return "http://%s" % host

    # One request's log row, in the making. A handler object serves every
    # request on a kept-alive connection, so these are reset for each.
    _log_method = ""
    _log_path = ""
    _bytes_in = 0
    _logged = True

    def _begin_log(self, method: str, path: str) -> None:
        self._log_method, self._log_path = method, path
        self._bytes_in, self._logged = 0, False

    def _log_before_answering(self, status: int, bytes_out: int) -> None:
        """Log the request now, before the client can see the answer.

        Written after the response, the row was not there yet for a client
        that asked `/_mock/requests` the moment its answer arrived - which is
        exactly what a test does, and on a slow runner it lost. Every answer
        goes out through `raw`, `_challenge` or the OPTIONS handler, and
        each calls this before `send_response`; the socket writer is not
        buffered, so the header block is what a client first sees. Once per
        request.

        Nothing is left for a later commit to take with it: every route
        returns its answer as its last act, after its own work is committed,
        and a failed one has been rolled back before its 500 is written.
        """
        if self._logged:
            return
        self._logged = True
        if self.config.log_requests:
            self._log_request_row(self._log_method, self._log_path, status,
                                  self._bytes_in, bytes_out)

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
                     self.headers.get("AS2-From", "") or "", db.wall_now()))
                self.mock.conn.commit()
                self.mock.requests_since_prune += 1
                if self.mock.requests_since_prune >= PRUNE_EVERY:
                    self.mock.prune()
        except sqlite3.Error:            # pragma: no cover - logging must not fail a request
            pass


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class _Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    mock: Mock

    def server_bind(self):
        # HTTPServer.server_bind reverse-resolves the address it just bound to
        # fill in `server_name`. Nothing here reads it, and on a host whose
        # resolver does not answer for 0.0.0.0 that lookup is the whole of a
        # minute before the port opens - by which time the Dockerfile's health
        # check, the CI smoke job and testing.Mock.start have all given up and
        # reported a start that failed for no visible reason (#136). The
        # address the socket reports is what the lookup was approximating.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

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
