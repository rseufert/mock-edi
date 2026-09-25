# mock-edi

[![CI](https://github.com/rseufert/mock-edi/actions/workflows/ci.yml/badge.svg)](https://github.com/rseufert/mock-edi/actions/workflows/ci.yml)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/mock-edi)](https://pypi.org/project/mock-edi/)

**A mock EDI trading partner.** Not an EDI library and not an AS2 server — the
thing on the *other end*. Send it an 850 and it sends back a 997, then an 855
that answers line by line, then an 856 with a shipment tree, then an 810 that
bills what shipped. Send it an EDIFACT `ORDERS` and the same thing happens in
`CONTRL` / `ORDRSP` / `DESADV` / `INVOIC`.

```
   you ──850──▶  mock-edi
       ◀──997──  the syntax parsed
       ◀──855──  2 lines: one confirmed, one short
       ◀──856──  shipment / order / item, with a tracking number
       ◀──810──  1132.80, terms 2% 10 net 30
```

There is plenty of open source for *speaking* EDI — OpenAS2 and mendelson will
terminate an AS2 connection, and a dozen libraries will parse an X12 segment.
What none of them is, is a counterparty. To test the code that runs when an
856 arrives unannounced, or when the invoice comes twice, or when the
acknowledgment never comes at all, you need a partner that does those things
on demand. Real ones do them on their own schedule, and getting one to do it
deliberately is a support ticket and a fortnight.

- **Zero dependencies.** Python 3.8+ standard library and SQLite, nothing else.
  It installs in a locked-down CI image.
- **Real wire shapes.** A 106-character fixed-width ISA that declares its own
  delimiters, EDIFACT's `?` release character, composite elements, `TDS` with
  two implied decimals, `HL` parent pointers, `AK3`/`AK4` error codes, MDNs
  with a `Received-Content-MIC`.
- **Failure on demand.** Short shipments, rejected lines, refused orders,
  duplicate invoices, a strict partner, and a partner that never answers —
  each one PATCH away.
- **It validates its own output.** Every document the mock writes is checked
  against the same dictionary it checks yours against. There is a test for it.

MIT licensed. ASC X12 and UN/EDIFACT are standards published by their
respective bodies; AS2 is RFC 4130. This project implements publicly
documented wire formats for testing purposes and is not affiliated with or
endorsed by any standards body or vendor.

By [Rick Seufert](https://rickseufert.com). The [projects page](https://rickseufert.com/#projects)
has this mock, [mock-sap](https://github.com/rseufert/mock-sap) and the worked examples
that use them together.

---

## Quick start

```bash
pip install mock-edi
mock-edi --port 8080
```

```bash
curl -X POST --data-binary @order.edi http://127.0.0.1:8080/edi
```

```json
{
  "accepted": true,
  "partner": "ACME",
  "dialect": "X12",
  "orders": ["4500000042"],
  "transactionSets": [
    {"code": "850", "control": "0001", "kind": "order", "accepted": true, "findings": []}
  ],
  "queued": [
    {"kind": "acknowledgment", "code": "997", "reference": "000000077", "dueAt": "..."},
    {"kind": "response",       "code": "855", "reference": "4500000042", "dueAt": "..."},
    {"kind": "despatch",       "code": "856", "reference": "4500000042", "dueAt": "..."},
    {"kind": "invoice",        "code": "810", "reference": "4500000042", "dueAt": "..."}
  ]
}
```

Then collect what it sent you:

```bash
curl "http://127.0.0.1:8080/_mock/mailbox?raw"
```

```
ISA*00*          *00*          *ZZ*MOCKEDI        *ZZ*ACME           *260924*1030*U*00401*000000001*0*T*>~
GS*PR*MOCKEDI*ACME*20260924*1030*2*X*004010~
ST*855*0002~
BAK*00*AD*4500000042*20260924****5100002*20260924~
...
PO1*1*100*EA*12.50**VP*WIDGET-001*UP*076123400003~
ACK*IA*100*EA*068*20260926~
```

Run it from a checkout with no install at all, or in a container:

```bash
python3 -m mockedi --port 8080
docker build -t mock-edi . && docker run -p 8080:8080 mock-edi
```

A guided tour of every endpoint, in curl:

```bash
bash examples/demo.sh
```

And an example of the code it exists to test: [`examples/po_bridge.py`](examples/po_bridge.py)
sends SAP purchase orders as 850s and posts the 855s back into SAP, and
[`examples/test_po_bridge.py`](examples/test_po_bridge.py) tests it against
this mock and [mock-sap](https://github.com/rseufert/mock-sap).
The other half of the same integration lives in mock-sap:
[`examples/invoice_check.py`](https://github.com/rseufert/mock-sap/blob/main/examples/invoice_check.py)
checks this mock's 810 invoices against the purchase order and the 856 ship
notice before posting them into SAP, and its tests cover a short shipment, a
price disagreement and the `duplicate-invoice` behaviour.

Both are walked through, test by test, in
[Testing an SAP-to-EDI Integration Without SAP or a Trading Partner](https://rickseufert.com/blog/2026/09/24/testing-an-sap-to-edi-integration).

## What it serves

| Surface | Endpoint |
| --- | --- |
| AS2 inbound | `POST /as2` — answers with an MDN, synchronous or asynchronous |
| Asynchronous MDN inbound | `POST /as2/mdn` — a partner's receipt for something the mock sent |
| Plain EDI inbound | `POST /edi` — the same pipeline, answering with a JSON summary |
| Validate only | `POST /_mock/validate` — findings, and nothing changed |
| Mailbox | `GET /_mock/mailbox` — collect what is waiting; `?leave` to peek, `?raw` for payloads |
| Outbox | `GET /_mock/outbox` — the queue, including what is not due yet |
| Release the queue | `POST /_mock/advance` — `?seconds=N` or `?all` |
| Send out of band | `POST /_mock/send` — replay an invoice, or send one unprompted |
| Partners | `GET/POST /_mock/partners`, `GET/PATCH/DELETE /_mock/partners/<id>` |
| Orders | `GET /_mock/orders`, `GET /_mock/orders/<po>` |
| Archive | `GET /_mock/documents`, `GET /_mock/interchanges`, `GET /_mock/interchanges/<id>?raw` |
| Receipts | `GET /_mock/mdns` |
| Outstanding documents | `GET /_mock/unacknowledged?older-than=60` |
| Work promised, not done | `GET /_mock/scheduled` |
| Directory trading | `GET /_mock/drop`, `POST /_mock/drop/scan` |
| The dictionary | `GET /_mock/dictionary`, `/_mock/dictionary/X12/850` |
| Health and state | `GET /_mock/health`, `GET /_mock/state`, `GET /_mock/requests` |
| Reset | `POST /_mock/reset` |
| Index page | `GET /` |

## The documents

| Business document | X12 | EDIFACT |
| --- | --- | --- |
| Purchase order | **850** | **ORDERS** |
| Purchase order response | **855** | **ORDRSP** |
| Purchase order change | **860** | **ORDCHG** |
| Change acknowledgment | **865** | **ORDRSP** |
| Despatch advice / ship notice | **856** | **DESADV** |
| Invoice | **810** | **INVOIC** |
| Syntax acknowledgment | **997** | **CONTRL** |

Both dialects are read and written from one dictionary
([`mockedi/schema.py`](mockedi/schema.py)), and one pipeline drives both, so
what you assert about an X12 flow holds for the EDIFACT one. `GET
/_mock/dictionary/X12/850` serves that dictionary as JSON — the actual rules,
not a description of them that can go stale.

Coverage is the commonly traded core of each set, not the full standard. A
real 850 admits some fifty segment types and almost nobody sends more than a
dozen; the mock implements the dozen, validates them properly, and reports an
unrecognised segment rather than pretending to understand it.

## Partner behaviours

Four partners are seeded. Change any of them at runtime:

```bash
curl -X PATCH -H 'Content-Type: application/json' \
     -d '{"behaviour":"short-ship"}' \
     http://127.0.0.1:8080/_mock/partners/ACME
```

A partner is refused anything the mock could not then act on: an unknown
field is named rather than dropped, a `version` has to match the dialect
(`004010` or `D:96A:UN`), `test` is a flag, `as2_url` needs a scheme the
courier can use, and an id has to fit the envelope — fifteen characters for
X12, because ISA06 is fixed at that width and a longer one would be truncated
to something the partner could never be found by.

| Behaviour | What the partner does |
| --- | --- |
| `accept` | Confirms everything in full and ships what was ordered. |
| `short-ship` | Confirms less than was ordered (`855` `IQ`, `ORDRSP` `QTY+83`), and ships and invoices the confirmed quantity. |
| `reject-line` | Refuses one line outright (`IR`) and leaves it out of the shipment and the invoice. |
| `reject-all` | Acknowledges the syntax, then refuses the order (`BAK` `RJ`). |
| `no-ack` | Says nothing at all. No 997, no 855. For testing your chase-up timer — the failure that actually costs money. |
| `duplicate-invoice` | Sends the invoice twice with the same invoice number, as a partner with a retry bug does. |
| `strict` | Rejects a transaction set for any finding, not only a fatal one. |

Some rules apply whatever the behaviour says, because they are what real
sellers actually do. The first that fires wins:

1. **A quantity of zero or less is rejected** (`IR`). Nothing downstream can
   make sense of a line that asks for nothing.
2. **An item that is not in the catalogue is rejected** (`IR`). The most
   common real rejection there is.
3. The partner's behaviour, above.
4. **Confirmed is capped at what is in stock** — `IQ` when it falls short,
   `IB` when there is none. This cap outranks the price rule, so a line that
   is both short *and* mispriced comes back `IQ` with the price named in its
   reason rather than changed in silence.
5. **A price the seller disagrees with is billed at the seller's price** and
   flagged `IP`. Price discrepancies are the commonest EDI dispute there is.

## Timing

By default every document is released the moment it is produced, so a test can
POST an order and read four documents back on the next line. Give them delays
when what you are testing is the waiting:

```bash
mock-edi --ack-delay 2000 --response-delay 30000 --invoice-delay 86400000
```

Nothing is released on a timer of its own. `POST /_mock/advance?all` releases
whatever is queued, whenever it was due — a test that has to sleep is slow and
flaky, and one that advances the clock is neither.

## AS2

```bash
curl -X POST --data-binary @order.edi \
  -H 'Content-Type: application/edi-x12' \
  -H 'AS2-From: ACME' -H 'AS2-To: MOCKEDI' \
  -H 'Message-ID: <po-1@acme.example>' \
  -H 'Disposition-Notification-To: edi@acme.example' \
  -H 'Disposition-Notification-Options: signed-receipt-protocol=optional, pkcs7-signature; signed-receipt-micalg=optional, sha256' \
  http://127.0.0.1:8080/as2
```

comes back as a `multipart/report` MDN with the MIC of what arrived:

```
Disposition: automatic-action/MDN-sent-automatically; processed
Received-Content-MIC: +H1EWvEMSJH/IHGsjy7c/dviFRwLgRoGBmxnTEbMkGA=, sha256
```

Name a `Receipt-Delivery-Option` and the response is `202` with the MDN posted
back to that URL instead.

**S/MIME is deliberately not implemented.** Signing and encrypting AS2
payloads needs certificates and a cryptography library, and this project has no
dependencies on purpose. A message that arrives encrypted or signed is refused
with an MDN saying exactly that, rather than being mangled. If your integration
must be tested against signed AS2, this mock is the wrong tool and will tell
you so on the first message.

## Making the mock come to you

A partner with no `as2_url` is a mailbox. Give one a URL and the mock stops
being something you poll and becomes something that *arrives*:

```bash
curl -X PATCH -H 'Content-Type: application/json' \
     -d '{"as2_url":"http://localhost:9000/as2"}' \
     http://127.0.0.1:8080/_mock/partners/ACME
```

Documents are then POSTed to your listener with AS2 headers, in the order they
were queued, and whatever MDN you return is recorded against them in
`/_mock/outbox`.

## Changing an order

A buyer changes an order it has already placed with an **860** (or an
**ORDCHG**, or an 850 restated with `BEG01 = 04`), and the seller answers with
an **865**. EDIFACT has no separate change acknowledgment message, so an
ORDCHG is answered by an **ORDRSP** — the difference most likely to catch out
someone porting a mapping from X12.

A restated 850 (`BEG01 = 04` or `05`) is read as the whole order: a line it
leaves out is deleted, and the 865 says so with `DI` — or refuses, if that
line has already shipped. For `05` (Replace) that is the only reading; for
`04` it is the mock's choice, because an 850 has no other way to drop a line.
To change some lines and leave the rest alone, send an 860.

```
POC*1*QD*60**EA*12.50**VP*WIDGET-001~     the buyer wants 60, not 100
ACK*IA*60*EA*068*20260926~                 the seller agrees
POC*2*DI*40**EA*4.15**VP*BRKT-050~         the buyer drops line 2
ACK*IR*0*EA~
REF*ZZ**Line deleted at the buyer's request~
```

**A change cannot unmake what has already happened.** A quantity cannot go
below what shipped, a shipped line cannot be deleted, an order that shipped
cannot be cancelled, and an order that has been invoiced cannot be changed at
all. A refused line comes back `IR` with the reason, and the order keeps what
it had — reporting the order's state instead would tell the buyer its request
succeeded.

**Nor promise what will never happen.** Between despatch and invoice a
quantity can still be raised and a line added, and what the 865 confirms then
ships as a **second consignment**: its own 856 carrying the difference, and its
own 810 naming it. Every consignment is billed by one invoice, so an order that
shipped twice is invoiced twice — which is the buyer-side matching worth
testing. A change that revives a cancelled order is packed and billed the same
way.

**Give yourself a window.** A change is only meaningful before the goods
leave, and with every delay at zero the order is invoiced before the POST
returns, so every change would be refused. That is correct behaviour, not a
limitation to work around:

```bash
mock-edi --despatch-delay 3600000 --invoice-delay 3600000
```

Delays postpone the *work*, not merely the posting. A despatch that is not due
yet has not been packed, so a change arriving in the meantime affects it —
which is the whole point, and why `GET /_mock/scheduled` shows work promised
but not done, separately from `/_mock/outbox`, which shows documents that
already exist.

## One file, several interchanges

A file from a VAN or an SFTP drop often holds more than one interchange, one
after another. Every one of them is read: each is its own envelope, with its
own control number, its own acknowledgment and its own verdict, so one being
refused says nothing about the rest. Each may even declare its own delimiters,
because the VAN concatenated what its senders gave it and they need not agree.

The summary keeps the shape it has for a single interchange — the keys beside
`interchanges` describe the whole payload, and `interchanges` describes each
one in turn:

```json
{
  "accepted": true,
  "orders": ["4500000042", "4500000043"],
  "interchanges": [
    {"interchange": "000000077", "accepted": true, "orders": ["4500000042"], "...": "..."},
    {"interchange": "000000078", "accepted": true, "orders": ["4500000043"], "...": "..."}
  ]
}
```

An interchange that is refused carries its own `error` and leaves the others
alone; the payload as a whole is `accepted` only when every interchange in it
was. Over AS2 one MDN answers the whole file, and says *processed* only when
all of it was. A dropped file is filed as processed on the same terms.

## A replayed interchange is refused

A retry bug on the sender's side is ordinary, and processing a duplicate order
is expensive. An interchange control number a partner has used before is
refused in the envelope's own words — a `TA1` with note code `025`, *duplicate
interchange control number*, or a `CONTRL` whose `UCI` carries `0085 = 26`,
*duplicate detected*.

```
TA1*000000077*260925*0820*R*025~
```

Nothing behind a refused envelope is read: no 997, no 855, and the order it
carried is not shipped and invoiced a second time. The replay is still
archived — refusing it is not forgetting it.

Control numbers belong to a pair of partners, so another partner may use the
same one. `--allow-duplicates` turns the check off for a test that wants the
older behaviour of replacing the order.

## Acknowledgments, both ways

The mock sends a 997 for everything it receives. It also *reads* one for
everything it sends, which is what makes the most expensive EDI failure
testable: nobody acknowledged my invoice.

```bash
curl "http://127.0.0.1:8080/_mock/unacknowledged?older-than=60"
```

```json
[
  {"code": "810", "kind": "invoice", "reference": "4500000042",
   "group_control": "4", "control": "0004", "partner": "ACME", "at": "..."}
]
```

Send a 997 back and the document it names is marked with the verdict:

```json
{"acknowledged": [
  {"code": "810", "control": "0004", "matched": true, "status": "rejected",
   "note": "BIG at segment 2: Segment has data element errors; element 4: Invalid code value ('BADPO')"}
]}
```

The two dialects address what they are acknowledging differently, and both are
matched properly. X12 names a *transaction set inside a functional group* —
`AK102` quotes GS06, `AK202` quotes ST02, and both are needed because ST02 is
only unique within its group. EDIFACT names a *message inside an interchange*,
with `UCI01` quoting UNB's control reference and `UCM01` quoting UNH01.

An acknowledgment naming something the mock never sent comes back
`"matched": false` rather than being silently dropped — it is real and common,
and usually evidence of the bug you are looking for.

Set a partner to `no-ack` and nothing is ever acknowledged, so
`/_mock/unacknowledged` keeps filling up. That is the point.

## Trading over a directory

Not all EDI is AS2. A great deal of it is still a folder: the partner writes a
file into it, you pick the file up; you write a file back, they pick it up.

```bash
mock-edi --drop-dir ./edi/in --pickup-dir ./edi/out
```

Anything dropped in `./edi/in` goes through the same pipeline a POST does, and
the answers are written into `./edi/out` as `<partner>-<code>-<control>.edi` —
written to a temporary name and renamed, so nothing watching the directory
ever sees a half-written file.

Two things every directory integration meets are handled rather than left to
bite. A file still being written is not read: anything modified within
`--drop-settle-ms` is left for the next pass, and `.tmp`, `.part` and dotfiles
are never read at all. A file that has been read is not read again: it is
moved into `processed/`, or into `failed/` if it could not be read — moved
rather than deleted, because a mock that eats the evidence is no use when a
test fails.

The poller runs every `--drop-interval-ms`, but it is not the only way in:

```bash
curl -X POST http://127.0.0.1:8080/_mock/drop/scan
```

scans once and returns what it found, so a test never has to wait for a poll
interval — the same reason `/_mock/advance` exists.

## Validation

Every inbound document is checked against the dictionary, and the findings
become a real 997 or CONTRL — `AK3`/`AK4` with X12 error codes, `UCS`/`UCD`
with EDIFACT ones. Ask for the findings as prose instead:

```bash
curl -X POST --data-binary @broken.edi http://127.0.0.1:8080/_mock/validate
```

```json
{
  "clean": false,
  "groupCode": "R",
  "explain": [
    "850/0001: rejected",
    "  BEG at segment 2: ZZ is not a code BEG01 accepts (00, 01, 04, 05, 06, 07, ...)",
    "  BEG at segment 2: BEG05 is not a valid date: '2026-09-24'",
    "  PO1 at segment 3 in the PO1 loop: PO102 must be a number, got 'ten'",
    "  SE01 counts 99 segments, the message holds 6 (4)"
  ]
}
```

Severity is the mock's own policy, and it is stated rather than implied. A
*fatal* finding rejects the transaction set — an unknown set, a missing
mandatory segment or element, a control number that does not match its
trailer, a segment count that does not add up, a line number used twice in
one order (the standards allow it; almost no implementation guide does). Everything else is accepted
with errors noted: an invalid code, a length violation, a malformed date, a
segment the set does not define. A partner set to `strict` rejects on either.

The envelope is checked too, and a fault there outranks anything inside it.
A `GE` whose count or control number disagrees with what arrived, or a group
with no `GE` at all, rejects the group: `AK9*R` with the standard's reason in
`AK905`. An `IEA` that disagrees with the `ISA`, or a file that stops before
its `IEA`, rejects the whole interchange, and the answer is a `TA1` with
`TA104 = R` and no 997, because no group inside it was read. A `TA1` also comes
back whenever `ISA14 = 1` asks for one - `kind=interchange-acknowledgment` in
the mailbox. An `ISA` element off its fixed width is noted (`TA104 = E`) but
the interchange is still read; that is the mock's choice, since receivers
differ. On the EDIFACT side a `UNZ` that miscounts, names another interchange
or never arrives rejects the interchange in the CONTRL's `UCI`, with `0085`
codes 29, 28 and 13.

Two limits, stated plainly: loop *membership* and repetition counts are
checked but loop *sequence* is not, and conditional requirements ("if PO104 is
present then PO103 must be") are not modelled. Both would need a rule language
to express, and the mock would rather leave them out than pretend.

## Layout

```
mockedi/schema.py        elements, segments, loops, transaction sets  (add shapes here)
mockedi/envelope.py      the shape both dialects share, and delimiter handling
mockedi/x12.py           reading and writing ASC X12 interchanges
mockedi/edifact.py       reading and writing UN/EDIFACT interchanges
mockedi/validate.py      checking a document against the dictionary
mockedi/ack.py           turning findings into a 997 or a CONTRL
mockedi/reconcile.py     reading an acknowledgment for something we sent
mockedi/transactions.py  business documents in, business documents out
mockedi/documents.py     what the seller decides, and the shipment and invoice
mockedi/partners.py      who we trade with, and how each one misbehaves
mockedi/pipeline.py      the choreography: an order in, four documents back
mockedi/delivery.py      posting to a partner that has somewhere to receive
mockedi/as2.py           AS2 headers, the MIC, and the MDN
mockedi/drop.py          trading over a directory rather than over HTTP
mockedi/db.py            SQLite: schema, number ranges, demo data
mockedi/server.py        HTTP: AS2, /edi, and the control plane
```

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) explains how they fit together;
[`docs/FILES.md`](docs/FILES.md) is an index of every file in the repository.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

494 tests, every one of them talking to a real mock over real HTTP. Nothing is
stubbed. The most valuable one is in `tests/test_dictionary.py`: every document
the mock generates is validated against the same dictionary it validates yours
with, so the day someone adds a segment to a writer and forgets the
definition, the suite says so.

## Extending it

Add a segment or a transaction set in `schema.py` and it is parsed, validated
and published in `/_mock/dictionary` without touching anything else. Add a
*behaviour* in `documents.decide()`. Add an endpoint in `server.py`.
[CONTRIBUTING.md](CONTRIBUTING.md) says where each kind of change goes and what
a good pull request carries.

## See also

[mock-sap](https://github.com/rseufert/mock-sap) — the same idea for SAP:
OData V2 and V4, BAPI/RFC and IDoc shapes over SQLite, also with zero
dependencies. An IDoc `ORDERS05` and an X12 850 are the same business
document, so the two mocks make a reasonable pair of ends for testing a
middleware layer. [`examples/po_bridge.py`](examples/po_bridge.py) is one, and
mock-sap's [`examples/invoice_check.py`](https://github.com/rseufert/mock-sap/blob/main/examples/invoice_check.py)
is another.

[Testing an SAP-to-EDI Integration Without SAP or a Trading Partner](https://rickseufert.com/blog/2026/09/24/testing-an-sap-to-edi-integration)
uses the two mocks together: purchase orders out and confirmations in, then
invoices checked against what was ordered and shipped, with the failure modes
each test exercises.

[rickseufert.com](https://rickseufert.com/#projects) lists both mocks side by
side, with the worked examples and how to run each one.
