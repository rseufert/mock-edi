"""A client for driving a mock from a test, so nobody has to write one.

The mock's control plane is plain HTTP and JSON, which is the point: any
language can drive it.  But a Python test that wants to say "send this order
and tell me what came back" should not first have to build a
`urllib.request.Request`, encode a dict, catch `HTTPError` and remember to
close it, and guess whether the body is JSON.  Every adopter was writing that,
and `examples/client.py` was where they copied it from.

    from mockedi.testing import Mock

    with Mock.start() as mock:                  # in this process, own port
        summary = mock.send(order)
        assert summary["accepted"]
        response = mock.document(partner="ACME", kind="response")
        assert response.find("ACK").get(1) == "IA"
        mock.settle()                           # nothing left undelivered

Or against one already running, anywhere:

    mock = Mock("http://127.0.0.1:8080")

Two mocks wired to each other - a seller and a buyer - are a conversation
rather than a request, and `exchange` runs it to a stop:

    seller.exchange(buyer)

Still no dependencies.  `Response` is a named tuple, so it reads as
`reply.status` and unpacks as `status, headers, body` - which is what let the
suite's own harness move onto this without touching a single test.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, NamedTuple, Optional

X12 = "application/edi-x12"
EDIFACT = "application/edifact"

# Generous: a slow CI runner is not a hung mock, and a request that gives up
# too early turns a slow machine into a mysterious failure.
TIMEOUT = 30.0


class MockError(Exception):
    """The mock answered, and said no.

    Carries the status and the parsed body, because the body is where the
    mock explains itself - a refusal names the field, the flag or the
    finding, and an exception that threw that away would be the one thing
    worse than no client at all.
    """

    def __init__(self, method: str, path: str, status: int, body: Any):
        self.method, self.path, self.status, self.body = method, path, status, body
        detail = body
        if isinstance(body, dict):
            detail = body.get("error") or body
        super().__init__("%s %s answered %d: %s" % (method, path, status, detail))


class Response(NamedTuple):
    status: int
    headers: Dict[str, str]
    body: Any


class Document:
    """A parsed interchange, with the search a test actually wants.

    Delegates everything to the `Interchange` underneath, so `.groups[0]` and
    the rest still work; `find` and `all` reach through the groups and
    messages, because "the ACK segment of the 855" is one thought and should
    not be three subscripts.
    """

    def __init__(self, payload: str):
        from . import edifact, x12 as x12_module
        from .envelope import sniff
        self.payload = payload
        self.interchange = (x12_module.parse(payload) if sniff(payload) == "X12"
                            else edifact.parse(payload))

    def __getattr__(self, name):
        return getattr(self.interchange, name)

    @property
    def segments(self) -> List[Any]:
        return [item for group in self.interchange.groups
                for message in group.messages for item in message.segments]

    def all(self, tag: str) -> List[Any]:
        """Every segment with this tag, in document order."""
        return [item for item in self.segments if item.tag == tag]

    def find(self, tag: str):
        """The first segment with this tag, or None."""
        found = self.all(tag)
        return found[0] if found else None

    # -- as a model
    #
    # `find("ACK").get(2)` is the right level for a test about one segment.
    # For a test about what the seller *said* - confirmed 90 of the 100 I
    # ordered, billed for 100 - a model reads better, and `transactions.py`
    # has the readers. These are the four documents a seller sends.

    def _message(self):
        for group in self.interchange.groups:
            for message in group.messages:
                return message
        raise ValueError("the interchange holds no message to read")

    def as_response(self):
        """An 855 or an ORDRSP, as a `transactions.Response`."""
        from . import transactions
        return transactions.read_response(self._message(),
                                          self.interchange.dialect)

    def as_change_response(self):
        """An 865, or an ORDRSP answering an ORDCHG."""
        from . import transactions
        return transactions.read_change_response(self._message(),
                                                 self.interchange.dialect)

    def as_despatch(self):
        """An 856 or a DESADV, as a `transactions.Despatch`."""
        from . import transactions
        return transactions.read_despatch(self._message(),
                                          self.interchange.dialect)

    def as_invoice(self):
        """An 810 or an INVOIC, as a `transactions.Invoice`."""
        from . import transactions
        return transactions.read_invoice(self._message(),
                                         self.interchange.dialect)

    def as_order(self):
        """An 850 or an ORDERS, as a `transactions.Order`."""
        from . import transactions
        return transactions.read_order(self._message(),
                                       self.interchange.dialect)

    def __repr__(self) -> str:
        return "<Document %s %s>" % (self.interchange.dialect,
                                     ",".join(self.interchange.codes()))


class Mock:
    """A mock, over HTTP, whether or not this process is running it."""

    def __init__(self, base: str = "http://127.0.0.1:8080",
                 timeout: float = TIMEOUT):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._httpd = None

    # -- lifecycle

    @classmethod
    def start(cls, timeout: float = TIMEOUT, **config) -> "Mock":
        """Start a mock in this process on a port the OS picks.

        `config` is `Config`'s own keywords, so a test needing a behaviour
        configures it here rather than reaching into the server afterwards.
        An in-memory database unless `db_path` says otherwise.
        """
        from .server import Config, make_server
        settings = dict(host="127.0.0.1", port=0, db_path=":memory:", quiet=True)
        settings.update(config)
        httpd = make_server(Config(**settings))
        mock = cls("http://127.0.0.1:%d" % httpd.server_address[1], timeout)
        mock._httpd = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return mock

    @property
    def server(self):
        """The `_Server` this client started, or None when it did not start one."""
        return self._httpd

    def close(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> "Mock":
        return self

    def __exit__(self, *exception) -> None:
        self.close()

    # -- HTTP

    def request(self, method: str, path: str, body: Any = None,
                headers: Optional[Dict[str, str]] = None,
                raw: bool = False) -> Response:
        """One request. Never raises for a status: the caller decides."""
        data = body
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str):
            data = data.encode()
        request = urllib.request.Request(
            self.base + path.replace(" ", "%20"), data=data, method=method)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                payload = reply.read()
                return Response(reply.status, dict(reply.headers),
                                payload if raw else _maybe_json(payload))
        except urllib.error.HTTPError as error:
            # Read *and* close: an unclosed HTTPError leaves a temporary file
            # behind, and a test that expects a 4xx should not also produce a
            # ResourceWarning.
            with error:
                payload = error.read()
            return Response(error.code, dict(error.headers),
                            payload if raw else _maybe_json(payload))

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw) -> Response:
        return self.request("POST", path, body, **kw)

    def patch(self, path: str, body: Any = None, **kw) -> Response:
        return self.request("PATCH", path, body, **kw)

    def delete(self, path: str, **kw) -> Response:
        return self.request("DELETE", path, **kw)

    def expect(self, method: str, path: str, body: Any = None,
               status: int = 200, **kw) -> Any:
        """The body of a request that answered as expected, or `MockError`."""
        reply = self.request(method, path, body, **kw)
        if reply.status != status:
            raise MockError(method, path, reply.status, reply.body)
        return reply.body

    # -- sending and collecting

    def send(self, payload: str, dialect: str = "") -> Dict[str, Any]:
        """POST an interchange to `/edi` and return the summary.

        The content type follows the document unless you name a dialect: a
        payload starting `UNA` or `UNB` is EDIFACT, anything else X12, which
        is the same sniff the mock itself does.
        """
        kind = dialect or ("EDIFACT" if payload.lstrip()[:3] in ("UNA", "UNB")
                           else "X12")
        return self.expect("POST", "/edi", payload,
                           headers={"Content-Type": EDIFACT if kind == "EDIFACT"
                                    else X12})

    def as2(self, payload: str, headers: Dict[str, str]) -> Response:
        """POST to `/as2`. Raw, because an MDN is not JSON."""
        return self.post("/as2", payload, headers=headers, raw=True)

    def mailbox(self, partner: str = "", kind: str = "",
                leave: bool = True) -> List[Dict[str, Any]]:
        """What is waiting for a partner. `leave` looks without collecting."""
        return self.expect("GET", "/_mock/mailbox" + _query(
            partner=partner, kind=kind, leave=leave or None))

    def document(self, partner: str = "", kind: str = "",
                 leave: bool = True) -> Document:
        """The one document of a kind waiting for a partner, parsed."""
        rows = self.mailbox(partner, kind, leave)
        if len(rows) != 1:
            raise MockError("GET", "/_mock/mailbox", 200,
                            {"error": "expected one %s for %s, found %d"
                                      % (kind or "document",
                                         partner or "anyone", len(rows))})
        return Document(rows[0]["payload"])

    def documents(self, **query) -> List[Dict[str, Any]]:
        return self.expect("GET", "/_mock/documents" + _query(**query))

    def outbox(self) -> List[Dict[str, Any]]:
        return self.expect("GET", "/_mock/outbox")

    def scheduled(self, pending_only: bool = False) -> List[Dict[str, Any]]:
        """Work the mock has promised: a despatch to pack, an invoice to write.

        Distinct from the outbox, which holds documents that already exist. A
        delay postpones the work, so what a test is waiting for is often here
        rather than there.
        """
        rows = self.expect("GET", "/_mock/scheduled" + _query(all=True))
        return [row for row in rows if not row["done_at"]] if pending_only \
            else rows

    def unacknowledged(self, older_than: Optional[float] = None) -> List[Dict[str, Any]]:
        query = {} if older_than is None else {"older-than": older_than}
        return self.expect("GET", "/_mock/unacknowledged" + _query(**query))

    # -- state

    def order(self, po_number: str, partner: str = "") -> Dict[str, Any]:
        """One order. `partner` says whose, when two partners use the number."""
        return self.expect("GET", "/_mock/orders/%s%s" % (
            po_number, "?partner=%s" % partner if partner else ""))

    def timeline(self, po_number: str, raw: bool = False,
                 partner: str = "") -> Dict[str, Any]:
        """Everything that happened to one order, in order."""
        query = "&".join(item for item in (
            "raw" if raw else "", "partner=%s" % partner if partner else "") if item)
        return self.expect("GET", "/_mock/orders/%s/timeline%s"
                           % (po_number, "?" + query if query else ""))

    def partners(self) -> List[Dict[str, Any]]:
        return self.expect("GET", "/_mock/partners")

    def partner(self, partner_id: str, **changes) -> Dict[str, Any]:
        """Read a partner, or change it when given something to change."""
        if changes:
            return self.expect("PATCH", "/_mock/partners/" + partner_id, changes)
        return self.expect("GET", "/_mock/partners/" + partner_id)

    def behaviour(self, partner_id: str, name: str) -> Dict[str, Any]:
        return self.partner(partner_id, behaviour=name)

    def findings(self, payload: str) -> List[str]:
        """What the mock makes of a document, as prose, changing nothing."""
        kind = ("EDIFACT" if payload.lstrip()[:3] in ("UNA", "UNB") else "X12")
        answer = self.expect("POST", "/_mock/validate", payload,
                             headers={"Content-Type": EDIFACT if kind == "EDIFACT"
                                      else X12})
        return answer.get("explain", [])

    def reset(self) -> Dict[str, Any]:
        return self.expect("POST", "/_mock/reset")

    def advance(self, seconds: Optional[float] = None, everything: bool = False,
                failed: bool = False) -> Dict[str, Any]:
        """Release what is due, everything, or what failed to be delivered."""
        if failed:
            return self.expect("POST", "/_mock/advance?failed")
        if everything:
            return self.expect("POST", "/_mock/advance?all")
        return self.expect("POST", "/_mock/advance" + _query(seconds=seconds))

    def exchange(self, *others: "Mock", timeout: float = 15.0,
                 passes: int = 10, advance: bool = True) -> None:
        """Push a conversation between this mock and others until it stops.

        `settle` drains one mock and waits for its own outbox. A conversation
        between two mocks is a rally: the seller's 855 arrives, the buyer
        answers with a 997, the seller reconciles it. Neither side is finished
        until both are, so settling either one returns with the other still
        holding work, and the number of alternating calls it takes is not
        something a test should have to know.

        Work that is only *promised* is the harder half. There are two kinds
        of not-yet and settling reaches neither: a `pending` row in the outbox,
        which `duplicate-invoice` produces for its second invoice, and an
        undone row in the schedule, which is where a delayed despatch or
        invoice waits - a delay postpones the work, not the posting, so the
        document does not exist yet to be pending. Either way the symptom is a
        document that appears to have been lost. So each pass moves every
        mock's clock before settling it.

        That means **`exchange` moves every clock it touches**, which is what a
        test about *what* is exchanged wants and the opposite of what a test
        about *when* wants. Pass `advance=False` to settle only what is already
        due, and drive the clock yourself.

        Raises rather than hanging if `passes` full rounds do not converge: a
        rally with no end is a bug in the test or in the mock, and the useful
        report is what each side was still holding.
        """
        mocks = (self,) + others
        for _ in range(passes):
            moved = False
            for mock in mocks:
                before = mock._outbox_state()
                if advance:
                    mock.advance(everything=True)
                mock.settle(timeout)
                # Movement, not advancing, is the signal. A promise that can
                # never be kept - a despatch for an order with nothing left to
                # ship - would otherwise advance for ever and make a sound flow
                # look like one that never ends.
                if mock._outbox_state() != before:
                    moved = True
            if not moved:
                return
        raise MockError("GET", "/_mock/outbox", 200,
                        {"error": "the mocks were still talking after %d "
                                  "passes" % passes,
                         "holding": {mock.base: mock._outbox_state()
                                     for mock in mocks}})

    def _outbox_state(self) -> List[Any]:
        """Enough of the outbox to tell whether anything moved."""
        return [(row["id"], row["status"]) for row in self.outbox()]

    def settle(self, timeout: float = 30.0) -> List[Dict[str, Any]]:
        """Wait until nothing in the outbox is still waiting to be delivered.

        Not a sleep and not a fixed number of drains: on some platforms a
        connection to a dead port takes seconds to be refused rather than
        failing at once, so the only reliable signal is the state itself.

        When this client started the mock it drains the courier first, which
        is faster and exact. Against a mock in another process there is no
        such handle, and polling is all there is.

        A partner with no `as2_url` is a mailbox: its documents stay `ready`
        until something collects them, and waiting for that to change would
        wait forever. Those are not undelivered - nobody undertook to deliver
        them - so they are not waited for.
        """
        if self._httpd is not None:
            self._httpd.mock.courier.drain(timeout)
        delivered_to = {partner["id"] for partner in self.partners()
                        if partner.get("as2_url")}
        deadline = time.time() + timeout
        rows: List[Dict[str, Any]] = []
        while time.time() < deadline:
            rows = self.outbox()
            waiting = [row for row in rows if row["status"] == "ready"
                       and row["partner"] in delivered_to]
            if not waiting:
                return rows
            time.sleep(0.05)
            rows = waiting
        raise MockError("GET", "/_mock/outbox", 200,
                        {"error": "still undelivered after %gs: %s"
                                  % (timeout, [(r["code"], r["status"])
                                               for r in rows])})


def _query(**parameters) -> str:
    """A query string from the parameters that were given a value.

    A flag - `leave`, `raw`, `all` - is a bare word in this control plane, not
    `?leave=true`, so a True value writes the name alone.
    """
    parts = []
    for name, value in parameters.items():
        if value is None or value is False or value == "":
            continue
        if value is True:
            parts.append(urllib.parse.quote(name, safe=""))
        else:
            # `safe=""` because a value is a value: a purchase order number
            # with a slash in it is one segment of a query, not two of a path.
            parts.append("%s=%s" % (urllib.parse.quote(name, safe=""),
                                    urllib.parse.quote(str(value), safe="")))
    return "?" + "&".join(parts) if parts else ""


def _maybe_json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return payload.decode("utf-8", "replace")
