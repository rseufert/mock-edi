# Changelog

Every release of [mock-edi](https://pypi.org/project/mock-edi/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

### Changed

- The package's **Homepage** link on PyPI now points to
  [rickseufert.com](https://rickseufert.com/#projects), which shows this mock,
  its counterpart and the worked examples together. **Repository** still
  points to GitHub.

## [0.2.0] - 2026-09-24

Three ways the mock was not yet a trading partner: it only spoke HTTP, it
never read the receipts it was sent, and an order once placed could not be
changed. All three are closed, and a delay now postpones the *work* rather
than merely the posting - which is what made the third possible. There is
also a worked example that drives mock-edi and mock-sap together.

### Added

- **An example integration, tested against both mocks.**
  `examples/po_bridge.py` reads a purchase order from SAP, sends it as an 850,
  and posts the 855 back into SAP as an `ORDRSP` IDoc, with
  [mock-sap](https://github.com/rseufert/mock-sap) standing in for SAP.
  `examples/test_po_bridge.py` covers a full confirmation, a short shipment, a
  rejected line, a partner that never answers, and an SAP outage while the
  855 is in hand - the test that shows why collecting from a mailbox has to
  be store-and-forward. CI runs it with the other documented examples.

- **Trading over a directory** ([#1]). `--drop-dir` is watched for inbound
  interchanges, which go through the same pipeline a POST does; `--pickup-dir`
  receives the answers as `<partner>-<code>-<control>.edi`. A great deal of
  real EDI is still a folder rather than an AS2 connection, and an integration
  whose inbound path is "watch a directory" had no way to be tested against
  the mock.

  The two traps every directory integration meets are handled rather than left
  to bite. A file still being written is not read - anything touched within
  `--drop-settle-ms` waits for the next pass, and `.tmp`, `.part` and dotfiles
  are never read - and the mock writes its own files to a temporary name and
  renames them, because recommending a convention it does not follow would be
  poor manners. A file that has been read is moved to `processed/`, or to
  `failed/` if it could not be read: moved rather than deleted, because a mock
  that eats the evidence is no use when a test fails.

  `POST /_mock/drop/scan` reads the directory immediately and says what it
  found, so a test never waits for a poll interval - the same reason
  `/_mock/advance` exists. `GET /_mock/drop` reports the directories, what is
  waiting and what the last scan did.

- **An acknowledgment is now read, not only sent** ([#2]). A 997 or CONTRL
  arriving for something the mock sent is matched against that document and
  records the verdict on it, so the most expensive EDI failure - *nobody
  acknowledged my invoice* - is finally testable.
  `GET /_mock/unacknowledged?older-than=60` asks the question an operations
  team actually asks, and `/_mock/documents?acknowledged=false` the same
  question in the archive's terms.

  The two dialects address what they acknowledge differently and both are
  matched properly: X12 names a transaction set *inside a functional group*,
  so `AK102` and `AK202` are both needed - ST02 is only unique within its
  group - while EDIFACT names a message inside the interchange `UCI01` quotes.
  An acknowledgment for something the mock never sent comes back
  `"matched": false` rather than being dropped; it is a real condition and
  usually evidence of the bug you are looking for.

- **Changing an order that has already been sent** ([#3]). An **860** or an
  **ORDCHG** - or an 850 restated with `BEG01 = 04` - changes an order the
  mock holds, and is answered with an **865**. EDIFACT has no change
  acknowledgment message of its own, so an ORDCHG is answered by an ORDRSP,
  which is the difference most likely to catch out somebody porting a mapping
  from X12.

  The rule that matters is that a change cannot unmake what has already
  happened: a quantity cannot go below what shipped, a shipped line cannot be
  deleted, a shipped order cannot be cancelled, and an invoiced order cannot
  be changed at all. A refused line comes back `IR` with its reason while the
  stored order keeps what it had - reporting the order's state instead would
  tell the buyer its request had succeeded.

- `GET /_mock/scheduled` shows work the seller has promised but not done,
  separately from `/_mock/outbox`, which shows documents that already exist.

### Changed

- **A delay now postpones the work, not merely the posting** ([#3]). The
  shipment and the invoice used to be created the instant the order arrived,
  with only their *delivery* deferred - so a change arriving during a despatch
  delay could never affect the despatch, and every change was refused against
  an order that was already invoiced. Fulfilment is now scheduled and carried
  out when it comes due, which is both what a seller does and what makes a
  change window exist at all.

  With the default delays of zero nothing observable changes: the work still
  happens before the request returns. With delays configured, a despatch that
  is not due yet has not been packed, and `/_mock/scheduled` says so.

### Fixed

- An outbound document recorded the *interchange* control number as its own
  ([#2]). ISA13 was being stored where ST02 belonged, so the archive disagreed
  with the document on the wire and an inbound 997 could never have been
  matched to anything. Outbound documents now record all three numbers - the
  interchange's, the group's and the transaction set's - and record them as
  they were written, four digits and all.
- A request could fail with `cannot commit - no transaction is active` when
  two clients wrote at once. The request log was the one database write not
  taken under the mock's lock, on the grounds that logging is harmless - but
  `commit()` commits the *connection*, not the statement, so a log entry
  written while another thread was mid-transaction committed that thread's
  work early and left its own commit with nothing to do. The request that had
  done the real work was the one that failed. Found by Python 3.9 on CI; 3.12
  and later hide it, because their sqlite3 no longer raises. There is now a
  test that drives six clients at once and asserts nothing answers 5xx.
- `POST /_mock/reset` did not clear scheduled work, so promises made before a
  reset were kept after it ([#3]). Found by a test that counted one release
  and got two.
- A drop directory configured with `--drop-settle-ms 0` never read anything
  ([#1]). A file's modification time can read very slightly *ahead* of the
  clock, so `mtime > now` was true for a file that had just been written and
  zero meant "never ready" rather than "no waiting". Found by Windows CI,
  where the two clocks disagree more often than they do elsewhere.
- Test classes that called `MockServerCase.setUpClass()` unbound were starting
  their server on the *base* class, so their own `config_kwargs` never
  applied. Nothing was wrong with the package; the delivery suite had simply
  not been running with the configuration it asked for.

## [0.1.0] - 2026-09-24

The first release: a mock EDI trading partner that answers an order with the
documents a real one sends.

### Added

- **The dictionary** (`schema.py`). Elements, segments, loops and transaction
  sets for ten transaction sets across both dialects, with the code lists that
  make validation mean something. Everything else is derived from it -
  parsing, validation, generation, and the dictionary published at
  `/_mock/dictionary`, which is therefore the rules themselves rather than a
  description of them that can drift.
- **ASC X12** (`x12.py`): the fixed-width ISA, with the delimiters read out of
  it rather than assumed; GS/GE functional groups; ST/SE transaction sets with
  the counts their trailers carry; 00401 and 00501, which differ in what ISA11
  means.
- **UN/EDIFACT** (`edifact.py`): the UNA service string advice, the `?`
  release character, composite elements, and an implicit functional group so
  that everything above the wire can treat both dialects alike.
- **850/ORDERS in, 997/855/856/810 and CONTRL/ORDRSP/DESADV/INVOIC out.** One
  pipeline drives both dialects, so what holds for an X12 flow holds for the
  EDIFACT one. Documents reference each other the way real ones do: the
  invoice names the shipment, the shipment names the order.
- **Validation** (`validate.py`, `ack.py`) against the dictionary, rendered as
  a real 997 (`AK3`/`AK4` with X12 error codes) or CONTRL (`UCS`/`UCD` with
  EDIFACT ones), and as prose for humans. The fatal-versus-noted policy is the
  mock's own, and is written down rather than left to be inferred.
- **Partner behaviours**: `accept`, `short-ship`, `reject-line`, `reject-all`,
  `no-ack`, `duplicate-invoice` and `strict`, changed at runtime through
  `/_mock/partners/<id>`. Two rules outrank them, because real sellers apply
  them too: an item not in the catalogue is rejected, and a price the seller
  disagrees with is billed at the seller's price and flagged `IP`.
- **AS2** (`as2.py`): `AS2-From`/`AS2-To`, synchronous and asynchronous MDNs,
  and the `Received-Content-MIC` under sha1, sha256 or sha512. S/MIME is
  deliberately absent - it needs certificates and a cryptography library, and
  this package has no dependencies - so a signed or encrypted payload is
  refused with an MDN that says so rather than mangled.
- **Delivery** (`delivery.py`): a partner with an `as2_url` has its documents
  POSTed to it, in the order they were queued, on one background thread, with
  the header names spelled the way AS2 spells them. `urllib` re-cases them,
  which is legal HTTP and an unusual spelling for AS2, so `http.client` is
  used instead.
- **A queue with due times** (`pipeline.py`). Every delay defaults to zero, so
  a test reads four documents back on the next line; configure real delays and
  release them with `POST /_mock/advance?all`, because a test that sleeps is
  slow and flaky and one that advances a clock is neither.
- **A control plane** under `/_mock`: health, state, partners, catalogue,
  orders, the document archive with raw payloads, the mailbox and outbox,
  `advance`, `send`, MDNs, the request log, `validate` and `reset`.
- **Deterministic demo data**: four partners covering the interesting
  behaviours, a twelve-item catalogue with valid UPC check digits, and two
  finished orders so the endpoints are not empty on a cold start.
- **228 tests**, every one of them over real HTTP. The one that matters most
  validates every document the mock *writes* against the dictionary it uses to
  check what it reads; it found six real bugs the first time it ran.

[#1]: https://github.com/rseufert/mock-edi/issues/1
[#2]: https://github.com/rseufert/mock-edi/issues/2
[#3]: https://github.com/rseufert/mock-edi/issues/3

[Unreleased]: https://github.com/rseufert/mock-edi/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rseufert/mock-edi/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rseufert/mock-edi/releases/tag/v0.1.0
