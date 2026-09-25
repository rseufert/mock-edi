# How mock-edi fits together

If you want to know *what each file is*, read [FILES.md](FILES.md). This
document is about why the pieces are shaped the way they are.

## The one idea

Everything is derived from a dictionary.

`schema.py` describes EDI the way the standards do: a document is an ordered
tree of segments and loops, a segment is an ordered list of elements, and an
element is a typed, length-bounded field that may carry a code list. Nothing
else in the package hard-codes a segment tag or an element position. The
parser names elements from it, the validator checks against it, the writers
build segments in the order it gives, and `/_mock/dictionary` serves it as
JSON — so the published rules *are* the rules, not a description of them that
can drift.

Adding a segment to a transaction set makes it parse, validate and appear in
the published dictionary, with no other change anywhere.

## Two dialects, one model

X12 and EDIFACT disagree about nearly everything at the surface: delimiters,
envelope tags, whether an element may be composite, whether there is an escape
character. They agree about the shape underneath, and that shape is what
`envelope.py` holds — `Seg`, `Message`, `Group`, `Interchange`.

```
              schema.py            the dictionary (both dialects)
                  │
              envelope.py          Seg / Message / Group / Interchange
                ╱     ╲
          x12.py       edifact.py  reading and writing the wire
                ╲     ╱
              validate.py          findings, not prose
                  │
               ack.py              997 or CONTRL, from the same findings
```

Above that line nothing knows which standard it is dealing with:

```
            transactions.py        Order / Line / Party  ⇄  segments
                  │
            documents.py           what the seller decides; shipment; invoice
                  │
             pipeline.py           order in ⟶ four documents out
                ╱   ╲
       delivery.py    server.py    posting out          AS2, /edi, /_mock
```

`pipeline.py` reasons about an *order*, a *response*, a *despatch* and an
*invoice*. Only `transactions.py` knows that an order is called 850 here and
ORDERS there, and it looks that up through `schema.set_code(dialect, kind)`.
This is why the test suite can assert that the same order sent in two dialects
produces the same total.

## Findings, not messages

`validate.py` produces structured findings — segment position, element
position, the standard's own reference number, an error code, a severity — and
never a sentence. That is because the same finding has to be rendered twice:
as an X12 `AK3`/`AK4` pair and as an EDIFACT `UCS`/`UCD` pair, which say the
same things with different numbers. `ack.py` does both renderings, and
`ack.explain()` renders a third one for humans, which is what `/_mock/validate`
returns and what a failing test prints.

Severity is the mock's own policy, not the standard's — the standards say what
is wrong, not what to do about it, and translators differ. It is written down
in `validate.py`'s docstring and in the README rather than left to be inferred.

## The walk

The one genuinely subtle piece of code is `validate._walk`. A loop's trigger
segment is also a *use* inside that loop — `N1` both starts the N1 loop and is
its first segment — so meeting `N1` again means the previous repetition ended,
not that the segment was used twice. The walker resolves outward from the
innermost open loop, checks for that case first, and finishes every loop it
passes on the way out, which is when a loop's missing mandatory segments are
reported.

Getting this wrong is not subtle in its effects: the first version reported
every second `N1` as an over-use, and the mock failed to validate its own
invoices.

## Why the queue exists

A trading partner does not answer four documents in one HTTP response. It
answers the acknowledgment in seconds and the invoice the next day.

So `pipeline._send` puts each document in the `outbound` table with a
`due_at`, and `release()` moves what is due to `ready`. With the default delays
of zero, everything is released before the POST returns and a test reads four
documents back on the next line. With real delays configured,
`POST /_mock/advance?all` releases them without anyone waiting — because a
test that sleeps is slow and flaky, and one that advances a clock is neither.

Nothing runs on a timer. That is deliberate.

## Two ways out

A partner with no `as2_url` is a **mailbox**: documents wait in `ready` until
`/_mock/mailbox` collects them. This is the right default, because the common
case is a test that drives the mock and reads back what it produced.

A partner *with* a URL gets its documents **posted**, by `delivery.Courier`, on
one background thread — not a thread per document, because a mock that answers
an 850 by opening four sockets at once is a good way to discover that your
listener is not thread safe and a bad way to discover anything else.

## State

SQLite, one connection, one `RLock` over everything that touches it. SQLite
itself copes with several threads on one connection; the mock's
read-modify-write sequences do not, and two interchanges arriving at once must
not be handed the same ISA13. The courier holds the same lock and releases it
before it makes a network call, so a partner that takes ten seconds to answer
does not stall the mock.

Money is stored as decimal strings and computed with `decimal.Decimal`.
Floating point would drift, and an invoice total that is a cent out is exactly
the bug an integration test exists to catch.

Control numbers are number ranges in a table, per partner, so they survive a
restart when the mock runs on a file database — duplicate control numbers are
a real trading-partner failure, and replaying one means being able to control
them.

A file database outlives the version of the mock that wrote it, so it is
upgraded in place when it is opened. `PRAGMA user_version` records the schema
version; `db.upgrade` creates any table the file lacks and adds any column the
schema declares that a table does not have - derived from `SCHEMA`, not
written out as a list of migrations - and only then builds the indexes. A file
from a newer mock is refused by name rather than misread. Every schema change
so far has been an addition; the first one that is not will need a step of its
own in `upgrade`.

## The property that keeps it honest

`tests/test_dictionary.py::GeneratedDocumentsAreValid` takes every document the
mock writes and validates it against the same dictionary it validates yours
with. A mock that cannot read its own output is not a trading partner, it is a
segment generator.

This test found six real bugs the first time it ran, five of them in the
dictionary rather than the writers — a missing code value, a component marked
mandatory inside an optional composite, a tracking number one character longer
than the element that held it.

## What is deliberately absent

- **S/MIME.** Signing and encryption need certificates and a cryptography
  library; the project has no dependencies on purpose. An encrypted payload is
  refused with an MDN that says so, rather than mangled.
- **Loop sequence validation.** Membership and repetition counts are checked;
  order within a loop is not.
- **Conditional requirements.** "If PO104 is present then PO103 must be" needs
  a rule language. The dictionary records M/O and treats C as O.
- **Functional groups in EDIFACT.** `UNG`/`UNE` is parsed if sent, never
  written; almost nobody uses it.

Each of these is a real part of EDI. Leaving them out and saying so is better
than a half-implementation that appears to work.
