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
BAK*00*AD*4500000042*20260924***20260924*5100002~
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
| The dictionary | `GET /_mock/dictionary`, `/_mock/dictionary/X12/850` |
| Health and state | `GET /_mock/health`, `GET /_mock/state`, `GET /_mock/requests` |
| Reset | `POST /_mock/reset` |
| Index page | `GET /` |

## The documents

| Business document | X12 | EDIFACT |
| --- | --- | --- |
| Purchase order | **850** | **ORDERS** |
| Purchase order response | **855** | **ORDRSP** |
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

| Behaviour | What the partner does |
| --- | --- |
| `accept` | Confirms everything in full and ships what was ordered. |
| `short-ship` | Confirms less than was ordered (`855` `IQ`, `ORDRSP` `QTY+83`), and ships and invoices the confirmed quantity. |
| `reject-line` | Refuses one line outright (`IR`) and leaves it out of the shipment and the invoice. |
| `reject-all` | Acknowledges the syntax, then refuses the order (`BAK` `RJ`). |
| `no-ack` | Says nothing at all. No 997, no 855. For testing your chase-up timer — the failure that actually costs money. |
| `duplicate-invoice` | Sends the invoice twice with the same invoice number, as a partner with a retry bug does. |
| `strict` | Rejects a transaction set for any finding, not only a fatal one. |

Two rules apply whatever the behaviour says, because they are what real
sellers actually do:

- an item that is not in the catalogue is rejected (`IR`), and
- a price the seller disagrees with is billed at the seller's price and
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
trailer, a segment count that does not add up. Everything else is accepted
with errors noted: an invalid code, a length violation, a malformed date, a
segment the set does not define. A partner set to `strict` rejects on either.

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
mockedi/transactions.py  business documents in, business documents out
mockedi/documents.py     what the seller decides, and the shipment and invoice
mockedi/partners.py      who we trade with, and how each one misbehaves
mockedi/pipeline.py      the choreography: an order in, four documents back
mockedi/delivery.py      posting to a partner that has somewhere to receive
mockedi/as2.py           AS2 headers, the MIC, and the MDN
mockedi/db.py            SQLite: schema, number ranges, demo data
mockedi/server.py        HTTP: AS2, /edi, and the control plane
```

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) explains how they fit together;
[`docs/FILES.md`](docs/FILES.md) is an index of every file in the repository.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

228 tests, every one of them talking to a real mock over real HTTP. Nothing is
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
middleware layer.
