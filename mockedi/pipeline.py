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

from . import (ack, charsets, claims, db, documents, edifact, partners,
               profiles, reconcile, remittance, schema, transactions, x12)
from .envelope import EdiSyntaxError, Interchange, Seg, sniff
from .transactions import Party
from .validate import (FATAL, ElementFinding, EnvelopeFinding, InterchangeReport,
                       SegmentFinding, validate)

PENDING = "pending"
# How much later the `late` partner answers than it otherwise would: an hour.
LATE_MS = 60 * 60 * 1000
READY = "ready"
DELIVERED = "delivered"
COLLECTED = "collected"
FAILED = "failed"

# The order documents leave in, and which behaviours suppress each one.
FOLLOW_UPS = (schema.RESPONSE, schema.DESPATCH, schema.INVOICE)

# What a supplier sends about an order the mock placed, and how to read it.

# The kinds of work a misbehaving *buyer* promises itself when a supplier's
# document arrives (#127). They name rows in `scheduled` beside the seller's
# despatch and invoice, so `/_mock/advance` releases them the same way.
CHANGE_LINE = "buyer-change"
CANCEL_ORDER = "buyer-cancel"

SUPPLIER_DOCUMENTS = {
    schema.RESPONSE: transactions.read_response,
    schema.CHANGE_RESPONSE: transactions.read_change_response,
    schema.DESPATCH: transactions.read_despatch,
    schema.INVOICE: transactions.read_invoice,
}

# Where each of them names the purchase order, for a finding that points at
# it: X12 in a header element, EDIFACT in RFF+ON's second component.
_X12_ORDER_REFERENCE = {"855": ("BAK", 3), "865": ("BCA", 3), "810": ("BIG", 4),
                        "856": ("PRF", 1)}


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
    # What a supplier sent about an order the mock placed, and which order.
    filed: List[Dict[str, str]] = field(default_factory=list)
    # The orders that were refused outright, by number, so that the 855 saying
    # so can be queued after the 997 rather than before it. Mirrors
    # `change_requests`: what arrived, kept until there is somewhere to answer.
    refused_orders: Dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.ok and bool(self.report) and self.report.accepted > 0


class Pipeline:
    """The mock's own behaviour as a trading partner."""

    def __init__(self, conn: sqlite3.Connection, config):
        self.conn = conn if isinstance(conn, db.UnitOfWork) else db.UnitOfWork(conn)
        self.config = config
        # How far `advance?seconds=` has moved the mock's clock past the real
        # one. Every due time, document date and MDN date reads `now()`, so
        # all of them see the moved clock; a reset puts it back.
        self.offset = datetime.timedelta(0)
        # Set by the server to the courier that posts documents to partners
        # who have an AS2 URL. Left unset, documents wait in the mailbox,
        # which is what a test without a listener of its own wants.
        self.on_release = None

    # -- identity

    @property
    def us(self) -> Party:
        return partners.us(self.config)

    def now(self) -> datetime.datetime:
        """The mock's clock: the real time in UTC, plus how far it was advanced.

        Aware, and the only clock anything above this reads. Everything it
        ends up written as goes through `db.stamp`, so that the string
        comparisons in `release` and in the `unacknowledged` cutoff are
        between values of the same shape.
        """
        return db.utcnow() + self.offset

    # -- inbound

    def receive(self, payload: bytes, transport: str = "http",
                message_id: str = "", mic: str = "",
                charset: str = "") -> List[Receipt]:
        """Read every interchange in the payload and queue the answers.

        One receipt per interchange, in the order they arrived. A payload
        usually holds one, and then this is a list of one; a file from a VAN
        or an SFTP drop may hold several, and each is a separate interchange
        with its own control number, its own acknowledgment and its own
        verdict - one being refused does not touch the others.

        All of it or none of it: an exception anywhere leaves the database as
        it was before the *payload* arrived. Documents released along the way
        are handed to the courier only once that is certain, so it never posts
        one that was rolled back.
        """
        notify, released = self.on_release, []
        if notify is not None:
            self.on_release = released.extend
        try:
            with self.conn.atomic():
                receipts = self._receive_all(payload, transport, message_id, mic,
                                             charset)
        finally:
            self.on_release = notify
        if notify is not None and released:
            notify(released)
        return receipts

    def _receive_all(self, payload: bytes, transport: str, message_id: str,
                     mic: str, charset: str = "") -> List[Receipt]:
        """Cut the payload into interchanges, then read each in its own charset.

        `charset` is what the transport said - HTTP's Content-Type - and only
        X12 needs it: an EDIFACT interchange declares its own in UNB.
        """
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
            charset = charset or "utf-8"
        # Viewed as ISO 8859-1 only to be cut: every byte survives the trip.
        view = payload.decode(charsets.BYTES)
        try:
            dialect = sniff(view)
            parts = x12.split(view) if dialect == "X12" else edifact.split(view)
        except EdiSyntaxError as error:
            return [Receipt(ok=False, error=str(error))]
        # The cut leaves out whatever follows the last trailer - usually a
        # newline. It belongs to the last interchange, so that the archived
        # rows put together are exactly the payload that arrived.
        tail = view[sum(len(part) for part in parts):]
        if tail and parts:
            parts[-1] += tail
        out = []
        for part in parts:
            raw = part.encode(charsets.BYTES)
            declared = charsets.declared(dialect, raw, charset)
            out.append(self._receive(charsets.decode(raw, declared), dialect,
                                     transport, message_id, mic, raw, declared))
        return out

    def _receive(self, text: str, dialect: str, transport: str,
                 message_id: str, mic: str, raw: Optional[bytes] = None,
                 charset: str = "") -> Receipt:
        try:
            interchange = (x12.parse(text) if dialect == "X12"
                           else edifact.parse(text))
        except EdiSyntaxError as error:
            return Receipt(ok=False, dialect=dialect, error=str(error))

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

        # Before it is stored, or it would find itself.
        faults = self._duplicate_fault(partner["id"], interchange)

        interchange_id = self._store_interchange(
            "in", interchange, partner["id"], text, transport, message_id, mic,
            raw, charset)

        role = partners.mock_role(partner)
        report = validate(interchange, strict=partner["behaviour"] == "strict",
                          envelope_faults=faults, role=role,
                          profile=profiles.load(self.conn, partner["id"]))
        if partner["behaviour"] == "reject-ack":
            # A translator misconfigured into refusing everything: every set
            # is rejected in the acknowledgment, and none is acted on. Its
            # acknowledgments of what the mock sent are still read.
            for item in report.messages:
                if item.kind != schema.ACKNOWLEDGMENT:
                    item.accepted, item.refused = False, True
        receipt = Receipt(interchange_id=interchange_id, partner=partner["id"],
                          dialect=dialect, control=interchange.control, report=report)

        for (_group, message), message_report in zip(interchange.messages(),
                                                     report.messages):
            reference = None
            if (message_report.kind in SUPPLIER_DOCUMENTS
                    and message_report.accepted):
                # Only a supplier's can get here: the role table refused the
                # same documents from a customer.
                reference = self._file(partner, message, message_report,
                                       dialect, receipt, interchange.control)
            self._record(interchange_id, partner, message, message_report,
                         dialect, reference)
            if (message_report.kind == schema.ORDER and message_report.accepted):
                order = transactions.read_order(message, dialect)
                if order.po_number:
                    # A buyer may restate a whole order rather than send an
                    # 860, and BEG01 says so. Against an order the mock already
                    # holds that is a change, not a replacement.
                    # This partner's own order with the number: another
                    # partner's order with it is a different order (#132).
                    known = documents.order_row(self.conn, order.po_number,
                                                partner["id"])
                    if order.purpose in transactions.CHANGE_PURPOSES and known:
                        held = [row["line"] for row in documents.order_lines(
                            self.conn, order.po_number, partner["id"])]
                        self._apply_change(
                            partner, transactions.change_from_order(order, held),
                            receipt)
                    elif known is not None and (
                            known["direction"] == documents.PLACED
                            or (known["status"] != documents.RECEIVED
                                and not self.config.allow_duplicates)):
                        # Two ways one number can already be spoken for.
                        #
                        # The mock placed an order under it with this partner
                        # when it was a supplier: recording this one would
                        # replace the mock's own purchase order with one it
                        # received.
                        #
                        # Or this partner's own order under it has been acted
                        # on - shipped, invoiced, cancelled, refused (#165).
                        # An order still only `received` may be restated, and
                        # that is useful; one already fulfilled cannot, because
                        # replacing it starts the fulfilment again and the mock
                        # ships and bills the whole order twice. A retry bug
                        # sends an original a second time, not a change, so
                        # nothing above catches it.
                        #
                        # Refused, rather than accepted and quietly not acted
                        # on, because "pick another number" is the only answer
                        # the sender can do anything with. `--allow-duplicates`
                        # turns that half off with the rest of #44's refusals:
                        # a flag that says "send me the same thing twice" has
                        # to mean it.
                        receipt.refusals.append(
                            {"order": order.po_number,
                             "reason": documents.NUMBER_IN_USE})
                        receipt.refused_orders[order.po_number] = order
                    else:
                        if known is not None:
                            # The order this one replaces was promised a
                            # despatch and an invoice, and this one is about
                            # to be promised its own. Left waiting, the first
                            # pair come due too, and the same shipment is
                            # advised twice (#200).
                            self._withdraw_fulfilment(partner, order.po_number,
                                                      "order restated")
                        documents.record_order(self.conn, partner, order,
                                               self.now())
                        receipt.orders.append(order.po_number)
            elif (message_report.kind == schema.CHANGE and message_report.accepted):
                change = transactions.read_change(message, dialect)
                self._apply_change(partner, change, receipt)
            elif (message_report.kind == schema.REMITTANCE
                  and message_report.accepted):
                # Acknowledged whatever its arithmetic says: what does not
                # add up is a business finding, beside the 997 (#156).
                message_report.disagreements.extend(remittance.record(
                    self.conn, partner, message, dialect, message_report.kind,
                    interchange.control, today=self.now().date()))
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

    def _duplicate_fault(self, partner_id: str, interchange: Interchange):
        """Refuse an interchange control number this partner has used before.

        A retry bug on the sender's side is common and processing a duplicate
        order is expensive, so a real receiver refuses the second copy in the
        envelope's own words rather than shipping the goods twice. The mock
        can already *produce* that bug with the `duplicate-invoice` behaviour;
        this is the other side of it, which is what a buyer's retry logic
        needs to be tested against.

        Turned off with `--allow-duplicates`, for a test that wants the older
        behaviour of replacing the order.
        """
        if self.config.allow_duplicates or not interchange.control:
            return []
        seen = db.one(self.conn,
                      "SELECT id, at FROM interchange WHERE direction = 'in'"
                      " AND partner = ? AND control = ? ORDER BY id LIMIT 1",
                      (partner_id, interchange.control))
        if seen is None:
            return []
        if interchange.dialect == "X12":
            return [EnvelopeFinding(
                code="025",            # I18: duplicate interchange control number
                tag="ISA", position=13,
                note="interchange control number %s was already received from "
                     "%s at %s" % (interchange.control, partner_id, seen["at"]))]
        return [EnvelopeFinding(
            code="26",                 # 0085: duplicate detected
            tag="UNB", position=5,
            note="interchange control reference %s was already received from "
                 "%s at %s" % (interchange.control, partner_id, seen["at"]))]

    def _file(self, partner: Dict[str, Any], message, message_report,
              dialect: str, receipt: Receipt, interchange_control: str) -> str:
        """Match a supplier's document to the order the mock placed with it.

        One that names an order the mock never placed with this supplier is
        refused - the one business disagreement that is also a 997's business,
        because with no order there is nothing to reconcile the document
        against (#116). Returns the PO number to archive it under, or "" for
        a refused one, which is no order's story.

        A document that is filed is also reconciled against the order (#126):
        where it disagrees goes on `message_report.disagreements`, which
        nothing that decides the 997 reads.
        """
        kind = message_report.kind
        document = SUPPLIER_DOCUMENTS[kind](message, dialect)
        po_number = document.po_number
        order = (documents.order_row(self.conn, po_number, partner["id"])
                 if po_number else None)
        if order is not None and order["direction"] == documents.PLACED:
            receipt.filed.append({"kind": kind, "code": message.code,
                                  "control": message.control, "order": po_number})
            message_report.disagreements.extend(claims.record(
                self.conn, partner, kind, message.code, message.control,
                interchange_control, document))
            self._promise_buyer_change(partner, kind, po_number)
            return po_number
        message_report.segments.append(
            _unknown_order(message, dialect, po_number, partner["id"]))
        message_report.accepted = False
        if not any(code == "5" for code, _ in message_report.set_errors):
            message_report.set_errors.append(("5", "one or more segments in error"))
        return ""

    def _record(self, interchange_id: int, partner: Dict[str, Any], message,
                message_report, dialect: str, reference: Optional[str] = None) -> str:
        """Log one inbound transaction set and what validation made of it."""
        # A set from the wrong direction is archived, as everything received
        # is, but not under the PO number it names: it is not part of that
        # order's story, and ?reference= would otherwise show a stranger's
        # invoice beside it.
        if reference is None:
            reference = ("" if message_report.misdirected
                         else _reference_of(message, dialect, message_report.kind))
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
            self._withdraw_fulfilment(partner, change.po_number,
                                      "order cancelled")
            return
        self._schedule_the_difference(partner, change.po_number, self.now())

    def _withdraw_fulfilment(self, partner, po_number, note: str) -> None:
        """Close what an order is still waiting for, unkept, and say why.

        The rows stay, marked done with the note, so `/_mock/scheduled?all`
        shows a promise that was withdrawn rather than one that never was.
        """
        self.conn.execute(
            "UPDATE scheduled SET done_at = ?, note = ?"
            " WHERE partner = ? AND po_number = ? AND done_at = ''",
            (db.now(), note, partner["id"], po_number))
        self.conn.commit()

    def _schedule_the_difference(self, partner, po_number, moment) -> None:
        """Keep what a change confirmed: pack and bill it if nothing will.

        A change that raises a shipped line, adds a line after despatch, or
        revives a cancelled or rejected order confirms goods no scheduled
        work is left to pack. The 865 has promised them, so a despatch and an
        invoice are scheduled for the difference - a second consignment, with
        an 856 and an 810 of its own. Work already scheduled is not doubled:
        a despatch still to come packs whatever is confirmed by then.
        """
        lines = documents.order_lines(self.conn, po_number, partner["id"])
        if not any(transactions.number(row["confirmed"])
                   > transactions.number(row["shipped"]) for row in lines):
            return
        waiting = {row["kind"] for row in db.rows(
            self.conn, "SELECT kind FROM scheduled WHERE partner = ?"
                       " AND po_number = ? AND done_at = ''",
            (partner["id"], po_number))}
        self._schedule_fulfilment(partner, po_number, moment,
                                  skip=waiting)

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
            order = documents.order_row(self.conn, po_number, partner["id"])
            if order is None:
                continue
            if behaviour == "out-of-order" and any(
                    transactions.number(row["confirmed"]) > 0
                    for row in documents.order_lines(self.conn, po_number,
                                                     partner["id"])):
                # The response waits until the goods have shipped; see
                # _fulfil. An order with nothing to ship has no despatch to
                # wait for, and is answered now.
                self._schedule_fulfilment(partner, po_number, moment)
                continue
            self._queue_response(partner, order, receipt, moment)
            if behaviour == "reject-all":
                continue
            self._schedule_fulfilment(partner, po_number, moment)

        for po_number, refused in receipt.refused_orders.items():
            # After the acknowledgment, like every other answer: the 997 says
            # the syntax was read, and the 855 says the order was not taken.
            self._queue_refusal(partner, refused, receipt,
                                documents.NUMBER_IN_USE, moment)

        for po_number in receipt.changes:
            order = documents.order_row(self.conn, po_number, partner["id"])
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
            answers_itself = schema.lookup("X12", schema.set_code(
                "X12", schema.ACKNOWLEDGMENT)).group
            for functional_id, control, version, messages in ack.group_reports(
                    interchange, report):
                if functional_id == answers_itself:
                    # A 997 is never acknowledged with a 997: two systems that
                    # both did so would answer each other for ever.
                    continue
                body = ack.functional_acknowledgment(
                    functional_id, control, version, messages,
                    report.group_errors.get((functional_id, control), []),
                    carries_version=self._x12_version(partner) >= "005010")
                self._send(partner, schema.ACKNOWLEDGMENT, body, interchange.control,
                           receipt, moment, delay, dialect="X12")
        else:
            # Nor is a CONTRL answered by a CONTRL. One travelling with
            # business messages is left out of the UCMs; an interchange of
            # nothing else gets no answer at all.
            messages = [m for m in report.messages
                        if m.kind != schema.ACKNOWLEDGMENT]
            if report.messages and not messages:
                return
            body = ack.syntax_report(interchange, report, messages)
            self._send(partner, schema.ACKNOWLEDGMENT, body, interchange.control,
                       receipt, moment, delay, dialect="EDIFACT")

    def _queue_refusal(self, partner, order, receipt, reason: str,
                       moment) -> None:
        """Answer an order the mock will not take, having stored none of it.

        A refusal is still an answer: the 855 rejects every line and says why,
        which is what a real seller sends and what a buyer's own retry logic
        waits for. Nothing was written to the database - that is the whole
        point - so the response is built from the order that arrived rather
        than from a stored row.
        """
        rows = [{"line": line.number, "sku": line.sku, "upc": line.upc,
                 "description": line.description,
                 "quantity": transactions.quantity_text(line.quantity),
                 "uom": line.uom,
                 "price": transactions.price_text(line.price),
                 "confirmed": "0", "status": transactions.REJECTED,
                 "reason": reason, "scheduled_on": ""}
                for line in order.lines]
        # The ship-to the buyer named, carried through: an 855 that refuses an
        # order still addresses the order it was sent, and leaving it out
        # writes an N1*ST with nothing in it.
        ship_to = order.ship_to
        stub = {"po_number": order.po_number,
                "ordered_on": (order.ordered_on.isoformat()
                               if order.ordered_on else ""),
                "currency": order.currency, "seller_order": "",
                "ship_to_name": ship_to.name, "ship_to_id": ship_to.identifier,
                "ship_to_street": ship_to.street, "ship_to_city": ship_to.city,
                "ship_to_region": ship_to.region,
                "ship_to_postal": ship_to.postal,
                "ship_to_country": ship_to.country}
        body = transactions.write_response(partner["dialect"], self.us, partner,
                                          stub, rows, moment)
        self._send(partner, schema.RESPONSE, body, order.po_number, receipt,
                   moment, self.config.response_delay_ms)

    def _queue_response(self, partner, order, receipt, moment) -> None:
        lines = documents.order_lines(self.conn, order["po_number"], order["partner"])
        body = transactions.write_response(
            partner["dialect"], self.us, partner, order, lines, moment)
        self._send(partner, schema.RESPONSE, body, order["po_number"], receipt,
                   moment, self.config.response_delay_ms)

    def _schedule_fulfilment(self, partner, po_number, moment,
                             skip: Sequence[str] = ()) -> None:
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
        work = [(schema.DESPATCH, self.config.despatch_delay_ms),
                (schema.INVOICE, self.config.invoice_delay_ms)]
        if partner["behaviour"] == "no-invoice":
            work = work[:1]
        elif partner["behaviour"] == "out-of-order":
            # The invoice first, due with the despatch: whichever runs first
            # packs, so the 810 goes out before the 856 advises the same
            # consignment.
            work = [(schema.INVOICE, self.config.despatch_delay_ms),
                    (schema.DESPATCH, self.config.despatch_delay_ms)]
        for kind, delay in work:
            if kind in skip:
                continue
            due = moment + datetime.timedelta(milliseconds=delay)
            self.conn.execute(
                "INSERT INTO scheduled (partner, po_number, kind, due_at, at)"
                " VALUES (?,?,?,?,?)",
                (partner["id"], po_number, kind, db.stamp(due), db.now()))
        self.conn.commit()

    # What a misbehaving buyer does when a supplier's document arrives: the
    # behaviour that answers it, and the kind of change it promises (#127).
    BUYER_CHANGES = {
        "change-after-confirm": (schema.RESPONSE, CHANGE_LINE),
        "cancel-late": (schema.DESPATCH, CANCEL_ORDER),
    }

    def _promise_buyer_change(self, partner: Dict[str, Any], kind: str,
                              po_number: str) -> None:
        """Promise the 860 a misbehaving buyer sends back at this document.

        A promise rather than a send, for the reason the whole `scheduled`
        table exists: it is released by `/_mock/advance` like everything else,
        so a test never sleeps, and the row is written inside the interchange's
        transaction - a failure later takes the promise with it, instead of
        leaving the mock owing an 860 for an interchange that never happened.

        Once only. A supplier that corrects its 855, or advises a second
        consignment, has not earned a second change; a buyer sends one.
        """
        wanted = self.BUYER_CHANGES.get(partner["behaviour"])
        if wanted is None or wanted[0] != kind:
            return
        promised = self.conn.execute(
            "SELECT 1 FROM scheduled WHERE partner = ? AND po_number = ?"
            " AND kind = ?", (partner["id"], po_number, wanted[1])).fetchone()
        if promised is not None:
            return
        self.conn.execute(
            "INSERT INTO scheduled (partner, po_number, kind, due_at, at)"
            " VALUES (?,?,?,?,?)",
            (partner["id"], po_number, wanted[1], db.stamp(self.now()),
             db.now()))

    def _send_buyer_change(self, row, moment,
                           receipt: Optional[Receipt] = None) -> None:
        """Keep one of those promises: write the 860 or ORDCHG and send it.

        It goes through `documents.change_placed`, so the order is changed in
        the same way a change asked for over `/_mock/purchase/<po>/change`
        changes it, and the mock's own view of what it asked for stays true.
        """
        partner = partners.get(self.conn, row["partner"])
        if partner is None:
            return
        po_number = row["po_number"]
        order = documents.order_row(self.conn, po_number, partner["id"])
        if order is None or order["status"] == "cancelled":
            return
        if row["kind"] == CANCEL_ORDER:
            request: Dict[str, Any] = {"cancel": True}
        else:
            lines = documents.order_lines(self.conn, po_number, partner["id"])
            first = next((line for line in lines
                          if transactions.number(line["quantity"]) > 1), None)
            if first is None:
                return
            request = {"lines": [{"line": first["line"], "action": "change",
                                  "quantity": str(int(
                                      transactions.number(first["quantity"]) // 2))}]}
        try:
            change = documents.change_placed(self.conn, po_number, partner["id"],
                                             request, moment)
        except (documents.Refused, LookupError):
            return
        order = documents.order_row(self.conn, po_number, partner["id"])
        body = transactions.write_change(partner["dialect"], self.us, partner,
                                         order, change, moment)
        self._send(partner, schema.CHANGE, body, po_number, receipt, moment)

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

        if row["kind"] in (CHANGE_LINE, CANCEL_ORDER):
            self._send_buyer_change(row, moment, receipt)
            return

        if row["kind"] == schema.DESPATCH:
            shipment = documents.create_shipment(self.conn, po_number,
                                                 partner["id"], moment)
            if shipment is None:
                return
            order = documents.order_row(self.conn, po_number, partner["id"])
            lines = documents.consignment_lines(self.conn, shipment["shipment_id"])
            body = transactions.write_despatch(
                partner["dialect"], self.us, partner, order, lines, shipment,
                moment)
            self._send(partner, schema.DESPATCH, body, po_number, receipt, moment)
            if partner["behaviour"] == "out-of-order" and not self._sent(
                    partner["id"], schema.RESPONSE, po_number):
                self._queue_response(partner, order, receipt, moment)
            return

        # Anything confirmed and not yet packed is packed now, the way a
        # seller who ships and invoices in one motion does: an invoice due
        # before the despatch, or before the second consignment of a quantity
        # raised after the first. The despatch, when it comes, advises that
        # consignment rather than packing another.
        documents.create_shipment(self.conn, po_number, partner["id"], moment)
        for shipment in documents.uninvoiced_shipments(self.conn, po_number,
                                                       partner["id"]):
            invoice = documents.create_invoice(
                self.conn, po_number, partner["id"], shipment["shipment_id"],
                moment, self.config.tax_rate)
            if invoice is None:
                continue
            order = documents.order_row(self.conn, po_number, partner["id"])
            lines = documents.consignment_lines(self.conn, shipment["shipment_id"])
            body = transactions.write_invoice(
                partner["dialect"], self.us, partner, order, lines, invoice,
                shipment, moment)
            self._send(partner, schema.INVOICE, body, po_number, receipt, moment)
            if partner["behaviour"] == "duplicate-invoice":
                # The same invoice number, sent twice, a few moments apart: a
                # partner with a retry bug, which is where duplicate-payment
                # incidents come from.
                self._send(partner, schema.INVOICE, body, po_number, receipt,
                           moment, 1000, note="duplicate of the invoice above")

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
                  documents.order_lines(self.conn, po_number, partner["id"])}
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
        delay_ms += self._lateness(partner)
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
            version = self._x12_version(partner)
            set_control = control.rjust(4, "0")
            message = x12.message(code, set_control, body, version)
            self._corrupt(partner, kind, message)
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
            self._corrupt(partner, kind, message)
            interchange = edifact.wrap(
                [message], self.config.as2_id, partner_id, interchange_control,
                sender_qualifier=self.config.qualifier,
                receiver_qualifier=partner["qualifier"], moment=moment,
                test=bool(partner["test"]))
            payload = edifact.render(interchange, newline=self.config.pretty)

        return self._enqueue(partner_id, dialect, code, kind, reference, payload,
                             interchange_control, group_control, set_control,
                             receipt, moment, delay_ms, note)

    @staticmethod
    def _lateness(partner) -> int:
        """What `late` adds to every delay: an hour, past any chase-up window
        worth the name, and on top of whatever the mock is configured with."""
        return LATE_MS if partner["behaviour"] == "late" else 0

    @staticmethod
    def _corrupt(partner, kind: str, message) -> None:
        """`corrupt`: a business document whose trailer miscounts by one.

        The one fault chosen, so a test knows what to expect: SE01 or UNT's
        count, which the receiving translator checks first and rejects the
        set for. The mock's acknowledgments are left intact - a corrupt 997
        would test something else entirely.
        """
        if partner["behaviour"] != "corrupt" or kind in (
                schema.ACKNOWLEDGMENT, schema.INTERCHANGE_ACKNOWLEDGMENT):
            return
        trailer = message.segments[-1]
        trailer.elements[0] = str(int(trailer.elements[0]) + 1)

    def _sent(self, partner_id: str, kind: str, reference: str) -> bool:
        return db.one(self.conn,
                      "SELECT id FROM outbound WHERE partner = ? AND kind = ?"
                      " AND reference = ? LIMIT 1",
                      (partner_id, kind, reference)) is not None

    @staticmethod
    def _x12_version(partner) -> str:
        """The X12 version the mock writes to a partner: its own, if the
        dictionary has it, and otherwise the one every set is declared at."""
        version = partner["version"] or ""
        return version if schema.supports("X12", version) else schema.VERSIONS["X12"][0]

    def _send_interchange_acknowledgment(self, partner: Dict[str, Any],
                                         interchange: Interchange,
                                         report: InterchangeReport,
                                         receipt: Optional[Receipt],
                                         moment: datetime.datetime,
                                         delay_ms: int = 0) -> Queued:
        """A TA1, in an interchange of its own with no functional group."""
        delay_ms += self._lateness(partner)
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
             db.stamp(due), note, db.now()))
        self.conn.commit()

        queued = Queued(id=int(cursor.lastrowid), kind=kind, code=code,
                        reference=reference, due_at=db.stamp(due), status=PENDING)
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
        order = (documents.order_row(self.conn, po_number, partner_id)
                 if po_number else None)
        if order is None:
            # Sending one partner's order to another is not a scenario, it is
            # a mistake - and a mock that performed it would let a test prove
            # something that could never happen on a real connection.
            others = [row["partner"] for row in
                      documents.orders_numbered(self.conn, po_number)]
            raise ValueError(
                "%s has no purchase order %r%s"
                % (partner_id, po_number,
                   "; %s does" % " and ".join(others) if others else ""))
        if order["direction"] == documents.PLACED:
            raise ValueError("purchase order %s is one the mock placed; the "
                             "supplier answers it, not the mock" % po_number)
        lines = documents.order_lines(self.conn, po_number, partner_id)

        if kind == schema.RESPONSE:
            body = transactions.write_response(
                partner["dialect"], self.us, partner, order, lines, moment)
        elif kind == schema.DESPATCH:
            shipment = documents.latest_shipment(self.conn, po_number, partner_id) or \
                documents.create_shipment(self.conn, po_number, partner_id,
                                          moment) or {}
            order = documents.order_row(self.conn, po_number, partner_id)
            lines = documents.order_lines(self.conn, po_number, partner_id)
            body = transactions.write_despatch(
                partner["dialect"], self.us, partner, order, lines, shipment, moment)
        elif kind == schema.INVOICE:
            shipment = documents.latest_shipment(self.conn, po_number, partner_id) or {}
            invoice = db.one(self.conn, "SELECT * FROM invoice WHERE partner = ?"
                                        " AND po_number = ?"
                                        " ORDER BY rowid DESC LIMIT 1",
                             (partner_id, po_number))
            if invoice is None:
                invoice = documents.create_invoice(
                    self.conn, po_number, partner_id, shipment.get("shipment_id", ""),
                    moment, self.config.tax_rate)
            if invoice is None:
                raise ValueError("nothing has shipped against %r, so there is "
                                 "nothing to invoice" % po_number)
            order = documents.order_row(self.conn, po_number, partner_id)
            lines = documents.order_lines(self.conn, po_number, partner_id)
            body = transactions.write_invoice(
                partner["dialect"], self.us, partner, order, lines, invoice,
                shipment, moment)
        else:
            raise ValueError("unknown document kind %r; known: %s"
                             % (kind, ", ".join(FOLLOW_UPS)))

        queued = self._send(partner, kind, body, po_number, None, moment, delay_ms)
        self.release(self.now())
        return queued

    # -- buying

    def place(self, partner_id: str, request: Dict[str, Any]) -> Tuple[Dict, Queued]:
        """Place an order with a supplier: store it, and send the 850 or ORDERS.

        It goes through `_send` like anything else the mock writes, so delays,
        `as2_url` delivery, the pickup directory and `/_mock/outbox` treat it
        as they treat an 855.
        """
        partner = self._supplier(partner_id)
        moment = self.now()
        order = documents.place_order(self.conn, partner, self.us, request, moment)
        body = transactions.write_order(
            partner["dialect"], self.us, partner, order,
            documents.order_lines(self.conn, order["po_number"], partner["id"]), moment)
        queued = self._send(partner, schema.ORDER, body, order["po_number"], None,
                            moment)
        if partner["behaviour"] == "duplicate-order":
            # The same order number, sent twice a moment apart: a buyer with a
            # retry bug, and the mirror of `duplicate-invoice` (#127). The
            # supplier must fulfil it once.
            self._send(partner, schema.ORDER, body, order["po_number"], None,
                       moment, 1000, note="duplicate of the order above")
        self.release(self.now())
        return order, queued

    def change_placed(self, po_number: str, partner_id: str,
                      request: Dict[str, Any]) -> Tuple[Dict, Queued]:
        """Change or cancel an order the mock placed, and send the 860 or ORDCHG."""
        order = documents.order_row(self.conn, po_number, partner_id)
        if order is None or order["direction"] != documents.PLACED:
            raise LookupError("the mock placed no purchase order %r with %s"
                              % (po_number, partner_id))
        partner = self._supplier(partner_id)
        moment = self.now()
        change = documents.change_placed(self.conn, po_number, partner_id,
                                         request, moment)
        order = documents.order_row(self.conn, po_number, partner_id)
        body = transactions.write_change(partner["dialect"], self.us, partner,
                                         order, change, moment)
        queued = self._send(partner, schema.CHANGE, body, po_number, None, moment)
        self.release(self.now())
        return order, queued

    def _supplier(self, partner_id: str) -> Dict[str, Any]:
        partner = partners.require(self.conn, partner_id)
        if partners.mock_role(partner) != schema.BUYER:
            raise documents.Refused(["the mock sells to %s; it does not order "
                                     "from it" % partner_id])
        return partner

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
                       " ORDER BY due_at, id", (db.stamp(when),))
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
        when = db.stamp(moment or self.now())
        due = db.rows(self.conn,
                      "SELECT * FROM outbound WHERE status = ? AND due_at <= ?"
                      " ORDER BY id", (PENDING, when))
        released: List[int] = []
        for row in due:
            raw, charset = self.wire(row)
            interchange_id = self._store_interchange_row(
                "out", row["dialect"], row["partner"], row["control"],
                row["payload"], "queue", row["message_id"], "", raw, charset)
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

    def redeliver(self, outbound_id: int = 0, partner_id: str = "") -> List[int]:
        """Hand failed deliveries back to the courier, unchanged.

        Not a resend: `/_mock/send` builds a *new* document with a *new*
        control number, which is a different event on the wire. This is the
        same bytes and the same control numbers going out a second time,
        which is what happens when a partner's listener was down and their
        AS2 software retried - and the only way to test that a listener is
        idempotent about a control number it has already seen.

        The document is not released again: it was released once, and its
        interchange and transaction set are already recorded. Only its
        delivery is tried again.

        Nothing here runs on a timer. A test that wants a retry asks for one.
        """
        clauses, params = ["status = ?"], [FAILED]
        if outbound_id:
            clauses.append("id = ?")
            params.append(outbound_id)
        if partner_id:
            clauses.append("partner = ?")
            params.append(partner_id)
        rows = db.rows(self.conn,
                       "SELECT id FROM outbound WHERE %s ORDER BY id"
                       % " AND ".join(clauses), tuple(params))
        if not rows:
            return []
        ids = [int(row["id"]) for row in rows]
        self.conn.execute(
            "UPDATE outbound SET status = ?, note = ? WHERE id IN (%s)"
            % ",".join("?" * len(ids)),
            tuple([READY, "redelivering"] + ids))
        self.conn.commit()
        if self.on_release is not None:
            self.on_release(ids)
        return ids

    def advance(self, seconds: float = 0.0, everything: bool = False) -> List[int]:
        """Move the mock's clock forward, and release what that makes due.

        The clock stays moved: two advances of 60 seconds release what is due
        in 90, and the documents written afterwards are dated by the moved
        clock. `everything` releases documents that are not due yet without
        moving the clock, which is what a test wants when it has configured a
        one-day invoice delay and does not intend to wait.
        """
        if everything:
            # Aware, like every other moment here: a naive max cannot be
            # converted to UTC without overflowing.
            return self.release(
                datetime.datetime.max.replace(tzinfo=datetime.timezone.utc))
        if seconds < 0:
            raise ValueError("the clock only moves forward; seconds must not "
                             "be negative, got %s" % seconds)
        self.offset += datetime.timedelta(seconds=seconds)
        return self.release(self.now())

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
                           message_id: str, mic: str, raw: Optional[bytes] = None,
                           charset: str = "") -> int:
        return self._store_interchange_row(
            direction, interchange.dialect, partner_id, interchange.control,
            payload, transport, message_id, mic, raw, charset)

    def _store_interchange_row(self, direction: str, dialect: str, partner_id: str,
                               control: str, payload: str, transport: str,
                               message_id: str = "", mic: str = "",
                               raw: Optional[bytes] = None, charset: str = "") -> int:
        cursor = self.conn.execute(
            "INSERT INTO interchange (direction, dialect, partner, control,"
            " transport, message_id, mic, payload, raw, charset, at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (direction, dialect, partner_id, control, transport, message_id, mic,
             payload, raw, charset, db.now()))
        self.conn.commit()
        return int(cursor.lastrowid)

    def wire(self, row) -> Tuple[bytes, str]:
        """An outbound document as it goes on the wire: its bytes and charset.

        The one place a document the mock wrote becomes bytes, so that what
        is posted, what lands in the pickup directory, what `?raw` returns and
        what the archive holds are the same bytes.
        """
        charset = charsets.for_outbound(self.conn, row["partner"], row["dialect"],
                                        row["payload"])
        return charsets.encode(row["payload"], charset), charset


def _reference_of(message, dialect: str, kind: str) -> str:
    """The document number an inbound transaction set is about."""
    if dialect == "X12":
        for tag, position in (("BEG", 3), ("BCH", 3), ("BAK", 3), ("BIG", 4),
                              ("BSN", 2), ("TRN", 2)):
            found = message.find(tag)
            if found is not None:
                return found.get(position)
        return ""
    bgm = message.find("BGM")
    return bgm.comp(2, 1) if bgm is not None else ""


def _unknown_order(message, dialect: str, po_number: str,
                   partner_id: str) -> SegmentFinding:
    """A finding at the element naming an order the mock never placed."""
    note = ("purchase order %s was never placed with %s" % (po_number, partner_id)
            if po_number else "names no purchase order placed with %s" % partner_id)
    if dialect == "X12":
        tag, position = _X12_ORDER_REFERENCE[message.code]
        component = 0
        found = message.find(tag)
    else:
        tag, position, component = "RFF", 1, 2
        found = next((item for item in message.segments
                      if item.tag == "RFF" and item.comp(1, 1) == "ON"), None)
    if found is None:
        # 720 code 3, a mandatory segment missing: without it there is no
        # order to hold the document against.
        return SegmentFinding(tag=tag, position=len(message.segments), code="3",
                              note=note, severity=FATAL)
    # 723 code 7 - which 0085 says as 12, Invalid value: an order number
    # that is not one of the buyer's is as wrong as a code not in its list.
    return SegmentFinding(
        tag=tag, position=found.position, code="8", note=note, severity=FATAL,
        loop="HL" if tag == "PRF" else "",
        elements=[ElementFinding(position=position, component=component,
                                 ref="324" if dialect == "X12" else "1154",
                                 code="7", value=po_number, note=note,
                                 severity=FATAL)])


def _findings(message_report) -> List[str]:
    out: List[str] = []
    for finding in message_report.segments:
        for element in finding.elements:
            out.append("%s@%d: %s" % (finding.tag, finding.position, element.note))
        if not finding.elements:
            out.append("%s@%d: %s" % (finding.tag, finding.position, finding.note))
    out.extend(note for _code, note in message_report.set_errors)
    return out
