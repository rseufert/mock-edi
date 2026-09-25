# Changelog

Every release of [mock-edi](https://pypi.org/project/mock-edi/). The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [semantic versioning](https://semver.org/spec/v2.0.0.html) -
while the major version is 0, a minor bump may change behaviour, and each entry
says so where it does.

## [Unreleased]

### Added

- **Python 3.14 is tested and declared** ([#20]). It had been out for a year
  with the matrix stopping at 3.13, so the interpreter where a removal would
  show up first was the one nobody watched. That entry runs with
  `PYTHONWARNINGS=error::DeprecationWarning`: with no dependencies, nothing
  but this project's own code can raise one.

- **A TA1 interchange acknowledgment** ([#51]), when `ISA14 = 1` asks for one
  and whenever the envelope itself is at fault. It travels in an interchange
  of its own with no functional group, and is collected from the mailbox as
  `kind=interchange-acknowledgment`.

### Changed

- **The partner control plane refuses what the mock cannot act on** ([#40]).
  It used to accept more than the wire could carry and say nothing: a
  misspelled field was dropped, `{"version": "5010"}` was written into `GS08`
  beside an `ISA12` of `00401`, `{"test": "maybe"}` was stored as prose, and a
  21-character id was accepted and then unreachable, because ISA06 truncates
  to fifteen and the partner could never be found by the id it sent.

  Now an unknown field is refused and *named*, beside the fields a partner
  actually has; `version` has to match the dialect; `test` is a flag;
  `mdn_mode` and `as2_url`'s scheme are checked; and ids and qualifiers have
  to fit the envelope, per dialect. Changing a long EDIFACT partner's dialect
  to X12 is refused rather than quietly breaking it.

  This is the "refuse rather than half-implement" rule applied to the control
  plane. A `PATCH` that answers 200 and changes nothing does not spare anyone
  trouble - it sends them looking for the bug somewhere else.

### Fixed

- **The asynchronous MDN was posted without its MIME boundary** ([#22]). The
  synchronous one went out with the headers `build_mdn` produced; the
  asynchronous one had its headers rebuilt by hand at delivery time, and the
  `boundary` parameter was not among them. A `multipart/*` with no boundary
  cannot be parsed by any MIME library, so OpenAS2 and mendelson log a
  malformed MDN and the message stays unacknowledged on their side - the
  opposite of what the sender asked for with `Receipt-Delivery-Option`. The
  reason it went unnoticed is that the test asserted on substrings of the
  body and never on the headers.

  The headers an MDN was built with are now kept with it and posted as they
  were, so `Date` and `MIME-Version` arrive too. A pending MDN written by an
  older version has its boundary read back out of its own body rather than
  guessed at. The test parses what was posted with the standard library's
  `email` package, so it fails if a MIME library cannot read it.

- The MDN's human-readable part declared `charset=us-ascii` and `7bit` while
  being written as UTF-8 ([#22]), so a partner id with a diaeresis in it put
  8-bit bytes in a part that promised none. The part now declares the charset
  and encoding it actually has.

- `signed-receipt-protocol=required` was answered with an unsigned
  `processed` MDN ([#22]) - a receipt the sender had already said it would
  not accept. RFC 4130 asks for a failure, so it now gets a `failed/Failure`
  MDN saying the mock cannot sign, and the interchange is not read.
  Refusing what it cannot do is this project's policy for S/MIME, and a
  required signed receipt is the same request by another name.
  `signed-receipt-protocol=optional` is unaffected.

- **A payload holding several interchanges was truncated to the first**
  ([#52]). `x12.parse` stopped at the first `IEA` and `edifact.parse` at the
  first `UNZ`, and everything after it was dropped without a word: one
  interchange stored, one 997 back, one order created, and no sign that the
  file had held two. Files with several interchanges in them are ordinary on
  a VAN and over SFTP, so a test could pass while half its input vanished.

  Every interchange in a payload is now read. Each is its own envelope - its
  own control number, its own acknowledgment, its own verdict - so one being
  refused leaves the rest alone, and each may declare its own delimiters,
  because a VAN concatenates what its senders gave it. The summary keeps the
  shape it has for a single interchange and gains an `interchanges` list
  describing them one by one; `/_mock/validate` does the same. Over AS2 one
  MDN answers the whole file and says *processed* only when every interchange
  in it was, and a dropped file is filed on the same terms.

  `Pipeline.receive()` returns a list of receipts rather than one, which
  matters to anyone driving the pipeline in process rather than over HTTP.

- A 997 with no `AK2` loop - `AK1` and `AK9` only, the commonest shape there
  is - validated clean and then acknowledged nothing ([#33]), so the
  document stayed on `/_mock/unacknowledged` for ever. `AK901` now applies to
  every set the mock sent in the group `AK102` names, of the kind `AK101`
  names. A CONTRL with `UCI` and no `UCM` does the same for every message in
  the interchange `UCI01` quotes.

- An inbound 997 was answered with a 997, and a CONTRL with a CONTRL
  ([#34]) - which the standards forbid, because two systems that both do it
  answer each other for ever. A functional group of `FA` is now read and not
  acknowledged, and a CONTRL is left out of the answer to the interchange it
  travels in; an interchange carrying nothing else gets no acknowledgment. A
  `TA1` asked for by `ISA14` is still sent: that answers the envelope.

- A partner id could contain a path ([#24]): `../../trav` was accepted, and
  its documents were written two directories above `--pickup-dir`. An id may
  now use only letters, digits, and `.`, `-` or `_` between them - narrower
  than X12 or EDIFACT allow, and said so, because an id is also a filename and
  a URL path. The pickup writer also refuses any name that would leave its
  directory, so a row from an older database cannot either, and lists what it
  refused in `/_mock/drop`.

- **An X12 envelope carrying no work was answered with silence** ([#55]). A
  functional group with no transaction set in it got no 997, because the
  acknowledgment was built from the messages that arrived rather than from
  the groups that did; an interchange with no group at all got nothing
  either. Worst of the three, an empty group *beside* a good one left the
  interchange accepted and one group unacknowledged, with nothing anywhere
  saying so - a sender cannot tell that from a group that never arrived.

  Now every group in the envelope gets a 997 of its own, an empty one
  carrying `AK9*R*0*0*0`; and an interchange with no functional group is
  refused by a `TA1` with note code 024, *Invalid Interchange Content*. A
  transaction set that arrived outside any `GS` is refused the same way
  rather than answered with `AK1*??*0`, which the mock's own dictionary
  rejected. **This changes behaviour:** a transaction set sent outside a
  functional group used to be processed, and is now refused.

  A `TA1` is the one interchange that holds no group by design, so the
  parser now keeps the segments between `ISA` and the first `GS` instead of
  discarding them, and an interchange whose whole content is a `TA1` is read
  as complete rather than empty.

- The README quoted a test count that had been wrong since 0.2.0 ([#17]) -
  228, against a suite of 454. `tools/check_docs.py` now asks the loader how
  many tests there are and fails when the README disagrees, so the number
  cannot drift again without CI saying so.

- On a file database, documents left `ready` for an AS2 partner when the
  mock stopped were never posted after it restarted ([#41]), and nor were
  asynchronous MDNs left `pending`: the courier learns of work only when it is
  released. Both are now put back on its queue at startup, in the order they
  were first queued. The suite has its first tests that restart a mock on the
  same file.

- A `--db` file written by 0.1.0 stopped the current version starting, with
  `no such column: ack_status` and no hint that the file was the cause
  ([#35]). A file database is now upgraded in place when it is opened:
  missing tables are created and missing columns added, from the schema
  itself, before any index is built; its version is recorded in
  `PRAGMA user_version`. A file from a newer mock is refused with a message
  naming it and both versions, and the process exits 2 instead of printing a
  traceback.

- An 850 restated with `BEG01 = 04` or `05` kept, confirmed and shipped any
  line it left out ([#38]), and the 865 never mentioned it. A line the
  restatement omits is now deleted, and the 865 answers it `DI` - or refuses
  it with the reason, if it has already shipped. For `04` this is a choice,
  and the README says so: to change some lines and leave the rest, send an
  860.

- **The 855 and 865 change on the wire** ([#49]). `BAK`, `BCH` and `BCA`
  were declared out of step with ASC X12 004010 from position 6 on, and the
  writers followed the declaration, so the 855 put its acknowledgment date in
  BAK07 (Contract Number) and the seller's order in BAK08 as though it were a
  request reference, and the 865 put the PO date in BCA09. They now go in
  BAK09, BAK08 (Reference Identification) and BCA10. The same fix stops a
  legitimate inbound 860 with a contract number in BCH08, or an 855 with one
  in BAK07, being reported as malformed. A mapping written against the old
  output will need its positions moving.

- A quantity raised or a line added after despatch was confirmed on the 865
  and then never shipped or billed ([#37]); nor was an order revived by a
  change after it had been cancelled. What a change confirms is now packed
  and billed: the difference ships as a second consignment with an 856 of
  its own, and **every consignment is invoiced separately**, each 810 naming
  its shipment. This changes behaviour for anyone who assumed one invoice
  per order - which a real seller does not promise either.

- Shutting the mock down could crash the interpreter with a segmentation
  fault ([#62]). `close()` stopped the courier and the drop poller with a
  join that gave up after two seconds and carried on regardless, then closed
  SQLite under whichever thread was still using it. The connection is now
  closed under the lock every thread takes around its database work, so a
  thread that has not stopped meets a closed database as an ordinary
  exception; `stop()` says whether the thread stopped; and `close()` names
  any that did not, on stderr and in what it returns.

- Two lines with the same number in one order were a 500 that left the
  database half-written ([#42]): the interchange was recorded, and if the PO
  number already existed its lines were deleted and not put back. A repeated
  line number (`PO101`, `POC01`, `LIN` 1082) is now a fatal finding, so the
  set is rejected by a 997 or CONTRL that says which line; and receiving an
  interchange is one unit of work, so any failure inside it rolls back
  everything it wrote. A handler that fails anywhere else is rolled back
  too, so the request log can no longer commit half of it.

- `/_mock/send` would send one partner's purchase order to another ([#40]).
  It never checked ownership, so ACME's order could be delivered to EURODIS as
  an 855 - letting a test prove something that could not happen on a real
  connection.

- Deleting a partner left its work behind ([#40]). Documents already waiting
  stayed in the mailbox addressed to somebody the mock no longer traded with,
  and promised shipments were still packed for them. Outstanding documents are
  now cancelled and outstanding promises marked, both noted `partner deleted`,
  and the delete reports how many of each. What was already done keeps its own
  history, and the document archive outlives the partner - it is the evidence
  a test came for.

- Envelope trailers are checked ([#51]). A `GE` or `IEA` that miscounted or
  named the wrong control number, a file cut off before its `IEA`, and an
  `ISA` off its fixed widths all came back clean with `AK9*A`. Now a group
  fault rejects the group, with `AK905` saying why (3, 4 or 5); an interchange
  fault rejects everything, and is answered by a `TA1` with `TA104 = R` and no
  997; and an `ISA` width is noted with `TA104 = E` but still read. **This
  changes behaviour:** an interchange with a bad trailer was acted on before,
  and is not now. A `UNZ` that miscounts, disagrees with `UNB` or is missing is
  the EDIFACT equivalent, rejected in the CONTRL's `UCI` with `0085` 29, 28 or
  13 - not 15/16 as the issue suggested, which mean "not supported in this
  position" and "too many constituents".

- An acknowledgment note read `errors;   element 4` - three spaces after the
  separator. The element detail carried a two-space indent that only makes
  sense on its own line, and these parts are joined inline. Cosmetic, but it
  is the text a person reads when a document they sent was refused.

- The CONTRL named the partner's directory in its `UNH` - `CONTRL:D:96A:UN`
  for a D.96A partner - where a service message names the syntax version:
  `CONTRL:D:3:UN` ([#50]). There is no CONTRL in D.96A, so a translator that
  matches acknowledgments on S009 would not have recognised the mock's, and a
  strict one would have rejected it. Business messages still use the
  partner's directory.

- The validator now compares an EDIFACT `UNH`'s message version and release
  with the directory its definition is declared in, and reports a mismatch as
  an error on `UNH` rather than reading the message against definitions it
  does not claim - which is how the wrong CONTRL passed the mock's own
  dictionary. An `ORDERS:D:01B:UN` is still accepted, now with that finding.
  X12 is not checked yet: every 005010 set is read against the 004010
  dictionary until [#46] makes it version-aware.

- A comma decimal mark declared in `UNA` is now honoured ([#53]). It was read
  and then ignored, so `UNA:+,? '` with `PRI+AAA:12,50'` - what German and
  Scandinavian partners send, and legal under ISO 9735 - was reported as
  "must be a number"; and an interchange written with `decimal=","` declared a
  comma in `UNA` and then wrote every number with a point. The mark is now
  translated in numeric (`R`) elements only, found through the dictionary, so
  a comma in a description is left alone. A point where `UNA` declares a comma
  is still accepted, as ISO 9735 allows either in data.

- **An order could end up with two shipments** ([#36]). The despatch and the
  invoice are separate pieces of scheduled work and either can come due
  first. The invoice has to pack the goods when nobody has yet - it needs
  something to bill - and the despatch then packed them again, giving the
  order two consignments with two bills of lading, and an 856 and an 810
  naming different ones. The cross-reference a buyer uses to match a bill to
  a delivery, quietly broken, and only in the ordering nobody runs by
  default.

  `create_shipment` now packs the difference between confirmed and shipped
  rather than everything confirmed, and returns the existing consignment when
  there is nothing left to pack. Whichever way round the delays are set, an
  order has one shipment and both documents name it.

- **`decide()` confirmed quantities nobody ordered** ([#39]). A line for zero
  or a negative quantity was confirmed `IA 1` under `short-ship`, because the
  floor of one unit was applied without looking at what was asked for; under
  `accept` it came back `IB "out of stock"` with four thousand in stock. A
  quantity of zero or less is now `IR` before any behaviour runs, and
  `short-ship` never confirms more than was ordered.

- **A quantity that is not a number is now fatal** ([#39]). `PO1*1*lots*EA`
  was a note rather than a rejection, the reader fell back to zero, and the
  order came back as a backorder with a stock reason that was not true. There
  is no reading of `lots` to carry forward, so there is nothing to accept.

- **An 850 with no lines was accepted and then contradicted itself** ([#39]).
  The 855 said `AD`, accepted with no change, while the stored order said
  `rejected`. The PO1 loop is mandatory in 004010 and is now declared so -
  and mandatory *loops* are now enforced at all, which they never were: the
  requirement was carried in the dictionary and read by nobody, so an 856
  with no HL hierarchy passed too.

- **The stock cap was an undocumented precedence rule** ([#39]). It outranks
  the price rule, so a line both short and mispriced reported only `IQ` and
  changed the price in silence. The cap is now in the precedence list in both
  the code and the README, and such a line names the price in its reason.

- **A replayed interchange is refused rather than fulfilled twice** ([#44]).
  The same `ISA13` from the same sender was accepted again: two shipments, two
  invoices, four more documents, and a conversation with somebody's accounts
  department. A retry bug on the sender's side is ordinary, so a real receiver
  refuses the copy in the envelope's own words - a `TA1` with note code `025`,
  or a `CONTRL` whose `UCI` carries `0085 = 27`. Nothing behind the refused
  envelope is read, and the replay is still archived, because refusing it is
  not forgetting it.

  The mock could already *produce* this bug with the `duplicate-invoice`
  behaviour; it can now detect one, which is what a buyer's retry logic needs
  to be tested against. `--allow-duplicates` restores the older behaviour.

  Two things fell out of it. A `TA1` was listed by `/_mock/unacknowledged` as
  a document awaiting a receipt, though nothing acknowledges a `TA1`; both
  acknowledgment kinds are now excluded. And the test builders reused one
  interchange control number for every order, which no real sender does - they
  now allocate a fresh one per call, and a test that wants a replay asks for
  it by passing the same number twice.

- The `CONTRL` refusing a replayed interchange named the wrong reason: `0085 =
  27` is "Security function not supported". "Duplicate detected" is `26`, which
  is what it now carries - so the entry above should read `0085 = 26`.




## [0.2.1] - 2026-09-25

No change to the package itself: the PyPI Homepage link now points at the
projects page that shows mock-edi, mock-sap and the worked examples together.

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
[#36]: https://github.com/rseufert/mock-edi/issues/36
[#39]: https://github.com/rseufert/mock-edi/issues/39
[#44]: https://github.com/rseufert/mock-edi/issues/44
[#40]: https://github.com/rseufert/mock-edi/issues/40
[#17]: https://github.com/rseufert/mock-edi/issues/17
[#20]: https://github.com/rseufert/mock-edi/issues/20
[#22]: https://github.com/rseufert/mock-edi/issues/22
[#42]: https://github.com/rseufert/mock-edi/issues/42
[#52]: https://github.com/rseufert/mock-edi/issues/52
[#55]: https://github.com/rseufert/mock-edi/issues/55
[#2]: https://github.com/rseufert/mock-edi/issues/2
[#3]: https://github.com/rseufert/mock-edi/issues/3
[#33]: https://github.com/rseufert/mock-edi/issues/33
[#34]: https://github.com/rseufert/mock-edi/issues/34
[#24]: https://github.com/rseufert/mock-edi/issues/24
[#35]: https://github.com/rseufert/mock-edi/issues/35
[#37]: https://github.com/rseufert/mock-edi/issues/37
[#38]: https://github.com/rseufert/mock-edi/issues/38
[#41]: https://github.com/rseufert/mock-edi/issues/41
[#46]: https://github.com/rseufert/mock-edi/issues/46
[#49]: https://github.com/rseufert/mock-edi/issues/49
[#50]: https://github.com/rseufert/mock-edi/issues/50
[#51]: https://github.com/rseufert/mock-edi/issues/51
[#53]: https://github.com/rseufert/mock-edi/issues/53
[#62]: https://github.com/rseufert/mock-edi/issues/62

[Unreleased]: https://github.com/rseufert/mock-edi/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/rseufert/mock-edi/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/rseufert/mock-edi/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rseufert/mock-edi/releases/tag/v0.1.0
