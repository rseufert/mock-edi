"""What happens when a document arrives, and what goes back.

This is the module that makes the mock a *trading partner* rather than an EDI
parser with a web server bolted on.  An 850 arrives and four things follow it,
in order, each a real document with real control numbers:

    850 in  ->  997 out   the syntax parsed
            ->  855 out   line by line, what the seller will supply
            ->  856 out   what is on the truck
            ->  810 out   what to pay

Each is queued with a delay, so a test can choose between "everything is there
by the time the POST returns" (the default, every delay zero) and "the
acknowledgment comes back in two seconds and the invoice tomorrow", which is
what the real thing does and what your timeout handling was written for.

`advance()` releases whatever is due.  Nothing is released on a timer of its
own, because a test that has to sleep is a test that is slow and flaky; a test
that calls `advance()` is neither.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import (ack, db, documents, edifact, partners, reconcile, schema,
               transactions, x12)
from .envelope import EdiSyntaxError, Interchange, Seg, sniff
from .transactions import Party
from .validate import InterchangeReport, validate

PENDING = "pending"
READY = "ready"
DELIVERED = "delivered"
COLLECTED = "collected"
FAILED = "failed"

# The order documents leave in, and which behaviours suppress each one.
FOLLOW_UPS = (schema.RESPONSE, schema.DESPATCH, schema.INVOICE)


@dataclass
class Queued:
    """One document the mock has decided to send."""
    id: int
    kind: str
    code: str
    reference: str
    due_at: str
    status: str


@dataclass
class Receipt:
    """What came of an inbound interchange."""
    ok: bool = True
    error: str = ""
    interchange_id: int = 0
    partner: str = ""
    dialect: str = ""
    control: str = ""
    report: Optional[InterchangeReport] = None
    queued: List[Queued] = field(default_factory=list)
    orders: List[str] = field(default_factory=list)
    changes: List[str] = field(default_factory=list)
    change_requests: Dict[str, Any] = field(default_factory=dict)
    change_outcomes: Dict[str, Any] = field(default_factory=dict)
    acknowledged: List[Dict[str, Any]] = field(default_factory=list)
    refusals: List[Dict[str, str]] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.ok and bool(self.report) and self.report.accepted > 0


class Pipeline:
    """The mock's own behaviour as a trading partner."""

    def __init__(self, conn: sqlite3.Connection, config):
        self.conn = conn
        self.config = config
        # Set by the server to the courier that posts documents to partners
        # who have an AS2 URL. Left unset, documents wait in the mailbox,
        # which is what a test without a listener of its own wants.
        self.on_release = None

    # -- identity

    @property
    def us(self) -> Party:
        return partners.us(self.config)

    def now(self) -> datetime.datetime:
        return datetime.datetime.now()

    # -- inbound

    def receive(self, payload: bytes, transport: str = "http",
                message_id: str = "", mic: str = "") -> Receipt:
        """Read an interchange, decide what it means, and queue the answers."""
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        try:
            dialect = sniff(text)
            interchange = (x12.parse(text) if dialect == "X12"
                           else edifact.parse(text))
        except EdiSyntaxError as error:
            return Receipt(ok=False, error=str(error))

        partner = partners.get(self.conn, interchange.sender)
        if partner is None:
            return Receipt(ok=False, dialect=dialect, control=interchange.control,
                           error="no trading partner is registered as %r; the "
                                 "interchange sender must match a partner id"
                                 % interchange.sender)
        if (self.config.strict_receiver and interchange.receiver
                and interchange.receiver != self.config.as2_id):
            return Receipt(ok=False, dialect=dialect, partner=partner["id"],
                           control=interchange.control,
                           error="the interchange is addressed to %r, this mock is %r"
                                 % (interchange.receiver, self.config.as2_id))

        interchange_id = self._store_interchange(
            "in", interchange, partner["id"], text, transport, message_id, mic)

        report = validate(interchange, strict=partner["behaviour"] == "strict")
        receipt = Receipt(interchange_id=interchange_id, partner=partner["id"],
                          dialect=dialect, control=interchange.control, report=report)

        for (_group, message), message_report in zip(interchange.messages(),
                                                     report.messages):
            reference = self._record(interchange_id, partner, message,
                                     message_report, dialect)
            if (message_report.kind == schema.ORDER and message_report.accepted):
                order = transactions.read_order(message, dialect)
                if order.po_number:
                    reference = order.po_number
                    # A buyer may restate a whole order rather than send an
                    # 860, and BEG01 says so. Against an order the mock already
                    # holds that is a change, not a replacement.
                    known = documents.order_row(self.conn, order.po_number)
                    if order.purpose in transactions.CHANGE_PURPOSES and known:
                        self._apply_change(
                            partner, transactions.change_from_order(order),
                            receipt)
                    else:
                        documents.record_order(self.conn, partner, order,
                                               self.now())
                        receipt.orders.append(order.po_number)
            elif (message_report.kind == schema.CHANGE and message_report.accepted):
                change = transactions.read_change(message, dialect)
                reference = change.po_number
                self._apply_change(partner, change, receipt)
            elif (message_report.kind == schema.ACKNOWLEDGMENT
                  and not message_report.envelope_rejected):
                # A receipt for something the mock sent, rather than something
                # for the mock to act on.
                for result in reconcile.apply(self.conn, partner["id"], dialect,
                                              message, interchange.control):
                    receipt.acknowledged.append({
                        "code": result.code, "control": result.control,
                        "groupControl": result.group_control,
                        "status": result.status, "verdict": result.verdict,
                        "note": result.note, "matched": result.matched,
                        "document": result.document_id,
                        "reference": result.reference,
                    })

        self._plan(partner, interchange, report, receipt)
        return receipt

    def _record(self, interchange_id: int, partner: Dict[str, Any], message,
                message_report, dialect: str) -> str:
        """Log one inbound transaction set and what validation made of it."""
        reference = _reference_of(message, dialect, message_report.kind)
        self.conn.execute(
            "INSERT INTO transaction_set (interchange_id, direction, dialect,"
            " partner, code, kind, control, group_control, reference, accepted,"
            " findings, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (interchange_id, "in", dialect, partner["id"], message.code,
             message_report.kind, message.control, message_report.group_control,
             reference, 1 if message_report.accepted else 0,
             json.dumps(_findings(message_report)), db.now()))
        self.conn.commit()
        return reference

    def _apply_change(self, partner, change, receipt: Receipt) -> None:
        """Hand a change to the seller, and remember what it decided.

        A refusal still produces an answer - that is the whole point of a
        change acknowledgment - so a refused change is recorded and reported
        rather than quietly dropped.
        """
        outcome = documents.apply_change(self.conn, partner, change, self.now())
        if outcome.refused:
            receipt.refusals.append({"order": change.po_number,
                                     "reason": outcome.reason})
            return
        receipt.changes.append(change.po_number)
        receipt.change_requests[change.po_number] = change
        receipt.change_outcomes[change.po_number] = outcome
        if outcome.cancelled:
            # Nothing more will be packed or billed for a cancelled order.
            self.conn.execute(
                "UPDATE scheduled SET done_at = ?, note = 'order cancelled'"
                " WHERE po_number = ? AND done_at = ''",
                (db.now(), change.po_number))
            self.conn.commit()

    # -- planning the answers

    def _plan(self, partner: Dict[str, Any], interchange: Interchange,
              report: InterchangeReport, receipt: Receipt) -> None:
        behaviour = partner["behaviour"]
        if behaviour == "no-ack":
            # The partner that says nothing. Everything below is skipped on
            # purpose: this is the failure that is hardest to test against a
            # real system, because a real system does not fail on request.
            return

        moment = self.now()
        self._queue_acknowledgment(partner, interchange, report, receipt, moment)

        for po_number in receipt.orders:
            order = documents.order_row(self.conn, po_number)
            if order is None:
                continue
            self._queue_response(partner, order, receipt, moment)
            if behaviour == "reject-all":
                continue
            self._schedule_fulfilment(partner, po_number, moment)

        for po_number in receipt.changes:
            order = documents.order_row(self.conn, po_number)
            if order is not None:
                self._queue_change_response(partner, order, po_number, receipt,
                                            moment)

        self.release(moment, receipt)

    def _queue_acknowledgment(self, partner, interchange, report, receipt,
                              moment) -> None:
        dialect = interchange.dialect
        delay = self.config.ack_delay_ms
        if dialect == "X12":
            # A TA1 when ISA14 asks for one, and whenever the envelope itself
            # is at fault, asked or not: that is the only place to say so.
            if interchange.ack_requested or report.interchange_findings:
                self._send_interchange_acknowledgment(
                    partner, interchange, report, receipt, moment, delay)
            if report.interchange_rejected:
                # Nothing inside a refused envelope was read, so there is no
                # group to acknowledge. The TA1 is the whole answer.
                return
            for functional_id, control, version, messages in ack.group_reports(report):
                body = ack.functional_acknowledgment(
                    functional_id, control, version, messages,
                    report.group_errors.get((functional_id, control), []))
                self._send(partner, schema.ACKNOWLEDGMENT, body, interchange.control,
                           receipt, moment, delay, dialect="X12")
        else:
            body = ack.syntax_report(interchange, report, report.messages)
            self._send(partner, schema.ACKNOWLEDGMENT, body, interchange.control,
                       receipt, moment, delay, dialect="EDIFACT")

    def _queue_response(self, partner, order, receipt, moment) -> None:
        lines = documents.order_lines(self.conn, order["po_number"])
        body = transactions.write_response(
            partner["dialect"], self.us, partner, order, lines, moment)
        self._send(partner, schema.RESPONSE, body, order["po_number"], receipt,
                   moment, self.config.response_delay_ms)

    def _schedule_fulfilment(self, partner, po_number, moment) -> None:
        """Promise to pack and to invoice, without doing either yet.

        The work is scheduled rather than done, because a despatch delay has
        to postpone the *packing* and not merely the posting.  A shipment
        created the instant the order arrives cannot reflect a change that
        arrives a minute later - and refusing every change would not be
        fidelity, it would be an artefact of having done the work too early.

        With the default delays of zero both promises come due immediately and
        are kept before the request returns, so nothing about the simple case
        changes.
        """
        for kind, delay in ((schema.DESPATCH, self.config.despatch_delay_ms),
                            (schema.INVOICE, self.config.invoice_delay_ms)):
            due = moment + datetime.timedelta(milliseconds=delay)
            self.conn.execute(
                "INSERT INTO scheduled (partner, po_number, kind, due_at, at)"
                " VALUES (?,?,?,?,?)",
                (partner["id"], po_number, kind, due.isoformat(), db.now()))
        self.conn.commit()

    def _fulfil(self, row, moment, receipt: Optional[Receipt] = None) -> None:
        """Keep one promise: pack the goods, or bill for them.

        A receipt is threaded through so that a document produced while
        answering a request still appears in that request's summary - with
        the default delays of zero, that is every document.
        """
        partner = partners.get(self.conn, row["partner"])
        if partner is None:
            return
        po_number = row["po_number"]

        if row["kind"] == schema.DESPATCH:
            shipment = documents.create_shipment(self.conn, po_number, moment)
            if shipment is None:
                return
            order = documents.order_row(self.conn, po_number)
            lines = documents.order_lines(self.conn, po_number)
            body = transactions.write_despatch(
                partner["dialect"], self.us, partner, order, lines, shipment,
                moment)
            self._send(partner, schema.DESPATCH, body, po_number, receipt, moment)
            return

        shipment = documents.latest_shipment(self.conn, po_number)
        if not shipment:
            # An invoice due before the despatch: bill what can be billed, the
            # way a seller who ships and invoices in one motion does.
            shipment = documents.create_shipment(self.conn, po_number, moment) or {}
        invoice = documents.create_invoice(
            self.conn, po_number, shipment.get("shipment_id", ""), moment,
            self.config.tax_rate)
        if invoice is None:
            return
        order = documents.order_row(self.conn, po_number)
        lines = documents.order_lines(self.conn, po_number)
        body = transactions.write_invoice(
            partner["dialect"], self.us, partner, order, lines, invoice,
            shipment, moment)
        self._send(partner, schema.INVOICE, body, po_number, receipt, moment)
        if partner["behaviour"] == "duplicate-invoice":
            # The same invoice number, sent twice, a few moments apart: a
            # partner with a retry bug, which is where duplicate-payment
            # incidents come from.
            self._send(partner, schema.INVOICE, body, po_number, receipt, moment,
                       1000, note="duplicate of the invoice above")

    def _queue_change_response(self, partner, order, po_number, receipt,
                               moment) -> None:
        """Answer the change, line by line, about the lines it asked about.

        The answer is about the *change*, not about the order: a line the
        seller refused to alter is reported refused here while the stored
        order keeps the quantity it already had. Reporting the order's state
        instead would tell the buyer its request succeeded.
        """
        change = receipt.change_requests.get(po_number)
        outcome = receipt.change_outcomes.get(po_number)
        stored = {row["line"]: row for row in
                  documents.order_lines(self.conn, po_number)}
        lines = []
        for entry in (outcome.lines if outcome else []):
            row = dict(stored.get(entry["line"], {}))
            if not row:
                row = {"line": entry["line"], "sku": "", "upc": "",
                       "description": "", "quantity": "0", "uom": "EA",
                       "price": "0.00", "confirmed": "0", "scheduled_on": ""}
            row["status"] = entry["status"]
            row["reason"] = entry["reason"]
            row["change_action"] = entry["action"]
            if entry["status"] == transactions.REJECTED:
                row["confirmed"] = "0"
            lines.append(row)
        if not lines:
            lines = list(stored.values())
        body = transactions.write_change_response(
            partner["dialect"], self.us, partner, order, lines, change, moment)
        self._send(partner, schema.CHANGE_RESPONSE, body, po_number, receipt,
                   moment, self.config.response_delay_ms)

    # -- outbound

    def _send(self, partner: Dict[str, Any], kind: str, body: Sequence[Seg],
              reference: str, receipt: Optional[Receipt],
              moment: datetime.datetime, delay_ms: int = 0,
              dialect: str = "", note: str = "") -> Queued:
        """Envelope a document, number it, and put it in the queue."""
        dialect = dialect or partner["dialect"]
        code = schema.set_code(dialect, kind)
        definition = schema.lookup(dialect, code)
        partner_id = partner["id"]

        control = str(db.next_number(self.conn, "transaction", partner_id))
        interchange_control = str(db.next_number(self.conn, "interchange", partner_id))
        group_control = ""
        # What goes on the wire, not what was drawn from the range: ST02 is
        # padded to four digits, and an acknowledgment will quote it back
        # exactly as it was written.
        set_control = control

        if dialect == "X12":
            group_control = str(db.next_number(self.conn, "group", partner_id))
            version = partner["version"] if partner["version"].isdigit() else "004010"
            set_control = control.rjust(4, "0")
            message = x12.message(code, set_control, body, version)
            interchange = x12.wrap(
                [message], self.config.as2_id, partner_id, interchange_control,
                group_control, definition.group,
                sender_qualifier=self.config.qualifier,
                receiver_qualifier=partner["qualifier"], version=version,
                interchange_version="00501" if version.startswith("005") else "00401",
                moment=moment, test=bool(partner["test"]))
            payload = x12.render(interchange, newline=self.config.pretty)
        else:
            # CONTRL is a service message: its UNH names the syntax
            # version (D:3:UN), not the directory the partner trades in.
            if kind == schema.ACKNOWLEDGMENT:
                version = definition.version
            else:
                version = partner["version"] if ":" in partner["version"] else "D:96A:UN"
            message = edifact.message(code, control, body, version)
            interchange = edifact.wrap(
                [message], self.config.as2_id, partner_id, interchange_control,
                sender_qualifier=self.config.qualifier,
                receiver_qualifier=partner["qualifier"], moment=moment,
                test=bool(partner["test"]))
            payload = edifact.render(interchange, newline=self.config.pretty)

        return self._enqueue(partner_id, dialect, code, kind, reference, payload,
                             interchange_control, group_control, set_control,
                             receipt, moment, delay_ms, note)

    def _send_interchange_acknowledgment(self, partner: Dict[str, Any],
                                         interchange: Interchange,
                                         report: InterchangeReport,
                                         receipt: Optional[Receipt],
                                         moment: datetime.datetime,
                                         delay_ms: int = 0) -> Queued:
        """A TA1, in an interchange of its own with no functional group."""
        partner_id = partner["id"]
        control = str(db.next_number(self.conn, "interchange", partner_id))
        version = interchange.version if interchange.version in ("00401", "00501") \
            else "00401"
        envelope = x12.wrap(
            [], self.config.as2_id, partner_id, control, "", "",
            sender_qualifier=self.config.qualifier,
            receiver_qualifier=partner["qualifier"], interchange_version=version,
            moment=moment, test=bool(partner["test"]))
        envelope.groups = []
        payload = x12.render(envelope, newline=self.config.pretty,
                             preamble=[ack.interchange_acknowledgment(interchange,
                                                                      report)])
        return self._enqueue(partner_id, "X12", "TA1",
                             schema.INTERCHANGE_ACKNOWLEDGMENT, interchange.control,
                             payload, control, "", "", receipt, moment, delay_ms)

    def _enqueue(self, partner_id: str, dialect: str, code: str, kind: str,
                 reference: str, payload: str, interchange_control: str,
                 group_control: str, set_control: str,
                 receipt: Optional[Receipt], moment: datetime.datetime,
                 delay_ms: int = 0, note: str = "") -> Queued:
        message_id = "<%s.%s@%s>" % (
            db.next_number(self.conn, "message"), code, self.config.as2_id)
        due = moment + datetime.timedelta(milliseconds=delay_ms)
        cursor = self.conn.execute(
            "INSERT INTO outbound (partner, dialect, code, kind, reference,"
            " payload, message_id, control, group_control, set_control, status,"
            " due_at, note, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (partner_id, dialect, code, kind, reference, payload, message_id,
             interchange_control, group_control, set_control, PENDING,
             due.isoformat(), note, db.now()))
        self.conn.commit()

        queued = Queued(id=int(cursor.lastrowid), kind=kind, code=code,
                        reference=reference, due_at=due.isoformat(), status=PENDING)
        if receipt is not None:
            receipt.queued.append(queued)
        return queued

    def send_document(self, partner_id: str, kind: str, po_number: str = "",
                      delay_ms: int = 0) -> Queued:
        """Send a document on demand, outside the usual choreography.

        `/_mock/send` uses this: it is how a test replays a lost invoice, or
        produces an unsolicited despatch advice, without arranging the whole
        order first.
        """
        partner = partners.require(self.conn, partner_id)
        moment = self.now()
        if kind == schema.ACKNOWLEDGMENT:
            raise ValueError("an acknowledgment answers an interchange; send the "
                             "interchange instead")
        order = documents.order_row(self.conn, po_number) if po_number else None
        if order is None:
            raise ValueError("no purchase order %r" % po_number)
        if order["partner"] != partner_id:
            # Sending one partner's order to another is not a scenario, it is
            # a mistake - and a mock that performed it would let a test prove
            # something that could never happen on a real connection.
            raise ValueError(
                "purchase order %s belongs to %s, not to %s"
                % (po_number, order["partner"], partner_id))
        lines = documents.order_lines(self.conn, po_number)

        if kind == schema.RESPONSE:
            body = transactions.write_response(
                partner["dialect"], self.us, partner, order, lines, moment)
        elif kind == schema.DESPATCH:
            shipment = documents.latest_shipment(self.conn, po_number) or \
                documents.create_shipment(self.conn, po_number, moment) or {}
            order = documents.order_row(self.conn, po_number)
            lines = documents.order_lines(self.conn, po_number)
            body = transactions.write_despatch(
                partner["dialect"], self.us, partner, order, lines, shipment, moment)
        elif kind == schema.INVOICE:
            shipment = documents.latest_shipment(self.conn, po_number) or {}
            invoice = db.one(self.conn, "SELECT * FROM invoice WHERE po_number = ?"
                                        " ORDER BY rowid DESC LIMIT 1", (po_number,))
            if invoice is None:
                invoice = documents.create_invoice(
                    self.conn, po_number, shipment.get("shipment_id", ""), moment,
                    self.config.tax_rate)
            if invoice is None:
                raise ValueError("nothing has shipped against %r, so there is "
                                 "nothing to invoice" % po_number)
            order = documents.order_row(self.conn, po_number)
            lines = documents.order_lines(self.conn, po_number)
            body = transactions.write_invoice(
                partner["dialect"], self.us, partner, order, lines, invoice,
                shipment, moment)
        else:
            raise ValueError("unknown document kind %r; known: %s"
                             % (kind, ", ".join(FOLLOW_UPS)))

        queued = self._send(partner, kind, body, po_number, None, moment, delay_ms)
        self.release(self.now())
        return queued

    # -- the queue

    def run_due(self, moment: Optional[datetime.datetime] = None,
                receipt: Optional[Receipt] = None) -> List[int]:
        """Do the work that has come due, before releasing anything.

        Ordered by due time and then by the order it was promised, so the
        despatch is packed before the invoice is written even when both come
        due at the same instant.
        """
        when = (moment or self.now())
        rows = db.rows(self.conn,
                       "SELECT * FROM scheduled WHERE done_at = '' AND due_at <= ?"
                       " ORDER BY due_at, id", (when.isoformat(),))
        done: List[int] = []
        for row in rows:
            self.conn.execute("UPDATE scheduled SET done_at = ? WHERE id = ?",
                              (db.now(), row["id"]))
            self.conn.commit()
            self._fulfil(row, when if when.year < 9999 else self.now(), receipt)
            done.append(int(row["id"]))
        return done

    def release(self, moment: Optional[datetime.datetime] = None,
                receipt: Optional[Receipt] = None) -> List[int]:
        """Do what is due, then mark everything ready to collect."""
        self.run_due(moment, receipt)
        when = (moment or self.now()).isoformat()
        due = db.rows(self.conn,
                      "SELECT * FROM outbound WHERE status = ? AND due_at <= ?"
                      " ORDER BY id", (PENDING, when))
        released: List[int] = []
        for row in due:
            interchange_id = self._store_interchange_row(
                "out", row["dialect"], row["partner"], row["control"],
                row["payload"], "queue", row["message_id"])
            # The transaction set's own control number, not the interchange's:
            # an inbound 997 quotes ST02 in AK202, and matching it against
            # ISA13 - which is what this recorded before - matches nothing.
            self.conn.execute(
                "INSERT INTO transaction_set (interchange_id, direction, dialect,"
                " partner, code, kind, control, group_control, reference,"
                " accepted, findings, at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (interchange_id, "out", row["dialect"], row["partner"], row["code"],
                 row["kind"], row["set_control"], row["group_control"],
                 row["reference"], 1, "", db.now()))
            self.conn.execute(
                "UPDATE outbound SET status = ?, released_at = ? WHERE id = ?",
                (READY, db.now(), row["id"]))
            released.append(int(row["id"]))
        self.conn.commit()
        if released and self.on_release is not None:
            self.on_release(released)
        return released

    def advance(self, seconds: float = 0.0, everything: bool = False) -> List[int]:
        """Move the clock forward for the queue, without moving it for anyone else.

        `everything` releases documents that are not due yet, which is what a
        test wants when it has configured a one-day invoice delay and does not
        intend to wait.
        """
        if everything:
            moment = datetime.datetime.max
        else:
            moment = self.now() + datetime.timedelta(seconds=seconds)
        return self.release(moment)

    def collect(self, partner_id: str = "", kind: str = "",
                leave: bool = False) -> List[Dict[str, Any]]:
        """Take ready documents out of the mailbox, oldest first.

        Collecting marks them collected, the way reading a mailbox does.
        `leave` looks without taking, for a control plane that should not
        change what it is reporting on.
        """
        clauses = ["status = ?"]
        params: List[Any] = [READY]
        if partner_id:
            clauses.append("partner = ?")
            params.append(partner_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        rows = db.rows(self.conn, "SELECT * FROM outbound WHERE %s ORDER BY id"
                       % " AND ".join(clauses), params)
        if not leave:
            for row in rows:
                self.conn.execute(
                    "UPDATE outbound SET status = ?, delivered_at = ?,"
                    " delivery = 'mailbox' WHERE id = ?",
                    (COLLECTED, db.now(), row["id"]))
            self.conn.commit()
        return rows

    # -- storage

    def _store_interchange(self, direction: str, interchange: Interchange,
                           partner_id: str, payload: str, transport: str,
                           message_id: str, mic: str) -> int:
        return self._store_interchange_row(
            direction, interchange.dialect, partner_id, interchange.control,
            payload, transport, message_id, mic)

    def _store_interchange_row(self, direction: str, dialect: str, partner_id: str,
                               control: str, payload: str, transport: str,
                               message_id: str = "", mic: str = "") -> int:
        cursor = self.conn.execute(
            "INSERT INTO interchange (direction, dialect, partner, control,"
            " transport, message_id, mic, payload, at) VALUES (?,?,?,?,?,?,?,?,?)",
            (direction, dialect, partner_id, control, transport, message_id, mic,
             payload, db.now()))
        self.conn.commit()
        return int(cursor.lastrowid)


def _reference_of(message, dialect: str, kind: str) -> str:
    """The document number an inbound transaction set is about."""
    if dialect == "X12":
        for tag, position in (("BEG", 3), ("BAK", 3), ("BIG", 4), ("BSN", 2)):
            found = message.find(tag)
            if found is not None:
                return found.get(position)
        return ""
    bgm = message.find("BGM")
    return bgm.comp(2, 1) if bgm is not None else ""


def _findings(message_report) -> List[str]:
    out: List[str] = []
    for finding in message_report.segments:
        for element in finding.elements:
            out.append("%s@%d: %s" % (finding.tag, finding.position, element.note))
        if not finding.elements:
            out.append("%s@%d: %s" % (finding.tag, finding.position, finding.note))
    out.extend(note for _code, note in message_report.set_errors)
    return out
