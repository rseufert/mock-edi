# Every file in this project

A guided index of the repository. If you are looking for *how the pieces fit
together* rather than *what each file is*, read [ARCHITECTURE.md](ARCHITECTURE.md)
first.

## Top level

| File | What it is |
| --- | --- |
| `docs/ARCHITECTURE.md` | How the pieces fit together and why they are shaped that way: the dictionary at the bottom, two dialects over one model, findings rather than messages, the loop walker, the queue, and what is deliberately absent. |
| `docs/FILES.md` | This file. |
| `README.md` | The user-facing documentation: quick start, the endpoint table, the documents, partner behaviours, AS2, validation, how to extend it. |
| `LICENSE` | MIT, verbatim, so GitHub and `licensee` detect it. The standards-body disclaimer lives in the README instead - appending anything to the licence text breaks that detection. |
| `pyproject.toml` | Packaging metadata and the **single source of truth for the version**. Declares the `mock-edi` console script and, notably, zero dependencies. |
| `CHANGELOG.md` | Every release, what it added and what it fixed, in Keep a Changelog form with links to the compare views. |
| `CONTRIBUTING.md` | What the project values and how to work on it: the principles that decide what gets merged, where to add each kind of thing, what a good pull request carries, and the release process. |
| `MANIFEST.in` | Adds the Dockerfile, examples and tests to the sdist; without it an sdist carries only the package itself. |
| `Dockerfile` | `python:3.12-slim`, `pip install .`, entrypoint bound to `0.0.0.0:8080`. Built and exercised by CI on every push. |
| `.gitignore` | Build output, virtualenvs, `*.db` files left behind by `--db`. |

## `mockedi/` - the package

Roughly in dependency order: `schema` sits at the bottom and depends on
nothing, `server` sits at the top and depends on everything.

| File | What it is | Edit it when |
| --- | --- | --- |
| `schema.py` | The dictionary, and the declarative heart. `Element`, `Segment`, `Use`, `Loop` and `TransactionSet` definitions for the ten transaction sets the mock speaks, in both dialects, with the code lists that make validation mean something. Parsing, validation, generation and the published dictionary are all derived from here. | Adding or changing a segment, an element, a code or a transaction set. |
| `envelope.py` | The wire-level model both dialects share - `Seg`, `Message`, `Group`, `Interchange` - plus delimiter handling, EDIFACT's release character, and the sniffing that decides which dialect a lump of bytes is. | Changing how documents are punctuated or split. |
| `x12.py` | Reading and writing ASC X12: the fixed-width ISA and the delimiters learned from it, GS/GE groups, ST/SE transaction sets, and the segment counts the trailers carry. | Changing X12 envelope handling. |
| `edifact.py` | Reading and writing UN/EDIFACT: the UNA service string advice, UNB/UNZ, UNH/UNT, and composite elements. | Changing EDIFACT envelope handling. |
| `validate.py` | Checking a document against the dictionary. The loop walker, the element checks, and the severity policy that decides whether a finding rejects a transaction set or is merely noted. | Adding a kind of check, or changing what counts as fatal. |
| `reconcile.py` | Reading an acknowledgment for something the mock *sent*: matching a 997 on its pair of control numbers and a CONTRL on the interchange it quotes, recording the verdict against the document, and reporting what is still outstanding. | Changing how receipts are matched. |
| `ack.py` | Turning a validation report into a 997 (AK1/AK2/AK3/AK4/AK5/AK9) or a CONTRL (UCI/UCM/UCS/UCD), and into prose for humans. | Changing acknowledgment shapes. |
| `transactions.py` | Business documents in, business documents out. The only module that knows an 850 keeps the order number in BEG03 and an ORDERS keeps it in BGM's C106/1004. Readers are forgiving, writers emit one documented profile. | Adding a document type, or changing what a generated document looks like. |
| `documents.py` | What the seller decides - which lines to confirm, how many, at what price - the shipment and invoice that follow, and what a change request is allowed to do to an order already placed. The decision rules and their precedence are documented in its docstring. | Changing the business decisions, the fulfilment documents, or the change rules. |
| `partners.py` | The partner records and the behaviours: who the mock trades with, and how each one misbehaves. Also the field specification - what a partner may be told, and which values the wire can actually carry - and the tidying up when one is deleted. | Adding a behaviour or a partner field. |
| `pipeline.py` | The choreography. An interchange arrives, is validated, recorded and answered; the answers are queued with due times, the work behind them is *scheduled* rather than done, and `advance()` does what has come due and then releases it. | Changing what answers what, or when. |
| `delivery.py` | The courier: posting released documents and asynchronous MDNs to partners that have a URL, on one background thread, with the header names spelled the way AS2 spells them. | Changing outbound delivery. |
| `as2.py` | AS2 headers, the MIC, and MDN construction and parsing. No S/MIME, by design and by explicit refusal. | Changing AS2 handling. |
| `drop.py` | Trading over a directory: the inbox the mock reads, the pickup directory it writes, the poller, and the two traps every directory integration meets - half-written files and files read twice. | Changing directory trading. |
| `db.py` | SQLite: the schema, the number ranges, and the deterministic demo data - four partners, a twelve-item catalogue with valid UPC check digits, and two finished orders. | Changing demo data, or adding a table. |
| `server.py` | The HTTP front end: `Config`, routing, the AS2 and plain endpoints, the whole `/_mock` control plane, the index page, and the lock that serialises database work. | Adding an endpoint or a configuration option. |
| `__init__.py` | Re-exports `Config` and `make_server`, and derives `__version__` from `pyproject.toml` in a checkout (reporting `…+source`) or from the installed metadata otherwise. | Rarely. |
| `__main__.py` | The `mock-edi` / `python -m mockedi` command line: argument parsing, the startup banner, and a readable message when the port is taken. | Adding a CLI flag. |

## `tests/` - one module per surface

Every test drives a real mock over real HTTP; nothing is stubbed. Run them all
with `python3 -m unittest discover -s tests -v`, or a single surface with
`python3 tests/test_choreography.py`.

| File | Covers |
| --- | --- |
| `support.py` | The shared harness, not a test module: `MockServerCase` starts a server on an ephemeral port in a background thread, resets it between tests, and provides `send`/`mailbox`/`document`/`order`/`behaviour` helpers, plus builders for X12 and EDIFACT orders. |
| `test_envelope.py` | Dialect sniffing, segment and element splitting, EDIFACT's release character round-tripping, trailing-element trimming, the 1-based accessors, and date parsing including the two-digit-year window. |
| `test_x12.py` | The fixed-width ISA (106 characters, padded identifiers, ISA11 differing between 00401 and 00501), delimiters learned from the document rather than assumed, group and transaction-set structure, and the trailer counts. |
| `test_edifact.py` | UNA written and read back, unusual punctuation, composites, delimiters escaped inside values, the implicit group, and message versions from UNH. |
| `test_validate.py` | Element checks and their codes, unknown and misplaced segments, missing mandatory segments, loop repetition versus over-use, trailer arithmetic, the fatal/error split, `strict`, and EDIFACT composite rules. |
| `test_integrity.py` | An interchange taken whole or not at all: a line number used twice rejected by the 997 or CONTRL rather than a 500, the order it restates left intact, and a real SQLite failure halfway through receiving leaving the database exactly as it was. |
| `test_shutdown.py` | Closing the mock with its background threads still at work: the connection is not closed while a thread holds the lock, and a courier stuck in a delivery is named rather than ignored and then meets a closed database as an ordinary exception, not a segmentation fault. |
| `test_trailers.py` | The envelope over HTTP: GE counts and control numbers rejecting a group in AK9, IEA mismatches and a file cut short answered by a TA1 and nothing else, a TA1 on request, ISA widths noted but read, and UNZ mismatches rejected in the CONTRL's UCI. |
| `test_ack.py` | The 997 - AK1 naming the group, AK3/AK4 carrying findings and a clipped copy of the bad data, AK5 and AK9 codes and counts, one acknowledgment per functional group - and the CONTRL equivalents. |
| `test_orders.py` | Reading the same order out of both dialects and getting the same answer, the forgiving readings a real partner needs, and the wire formats for quantities, prices and implied decimals. |
| `test_choreography.py` | The whole point: 850 in and 997/855/856/810 back, ORDERS in and CONTRL/ORDRSP/DESADV/INVOIC back, the documents referencing each other, both dialects agreeing, and control numbers advancing per partner. |
| `test_behaviours.py` | Every partner behaviour, the rules that outrank them (unknown item, disputed price), and stock limits. |
| `test_as2.py` | Synchronous and asynchronous MDNs, the MIC under each digest, the human-readable part, refusals (S/MIME, wrong recipient, unregistered sender), and receipts coming back in. |
| `test_partners.py` | The partner control plane refusing what it cannot act on: unknown fields named rather than dropped, `version` per dialect, the `test` flag, `mdn_mode`, `as2_url`'s scheme, qualifier and id widths, sending one partner's order to another, and what becomes of a deleted partner's outstanding work. |
| `test_control.py` | Health and state, partner CRUD, the mailbox and its flags, delays and `advance`, sending out of band, the archive, reset, 404s, and basic authentication. |
| `test_dictionary.py` | The model's own invariants, the published dictionary, `/_mock/validate` - and `GeneratedDocumentsAreValid`, which checks every document the mock writes against the dictionary it checks yours against. |
| `test_change.py` | Changing an order already sent: quantities up and down, lines added and deleted, cancellation, the refusals (after the invoice, below what shipped, an order we never saw), an 850 restated as a change, and the EDIFACT side answered by an ORDRSP. |
| `test_reconcile.py` | Acknowledgments coming back: a 997 marking a document accepted, rejected or accepted-with-errors, matching that needs both control numbers, an acknowledgment for something we never sent, the CONTRL equivalents, `/_mock/unacknowledged`, and that the control numbers recorded are the ones actually on the wire. |
| `test_drop.py` | Reading a drop directory and writing a pickup directory: the same pipeline as a POST, files moved to `processed/` and `failed/`, half-written files left alone, temporary and hidden names ignored, and one test for the poller thread. |
| `test_concurrency.py` | Several clients at once: concurrent writes and control-plane reads, asserting nothing answers 5xx. The one timing test in the suite, and it exists because the request log was committing other threads' transactions out from under them. |
| `test_delivery.py` | Posting to a real listener: order, AS2 headers and their exact spelling, receipts recorded, failures recorded rather than raised, and mailbox partners left alone. |

## `tools/` - the checks CI runs beside the tests

| File | What it is |
| --- | --- |
| `check_docs.py` | Fails if a tracked file has no row in `docs/FILES.md`, if a row names a file that no longer exists, or if a module is missing from the README's layout block. It checks coverage, not prose. |
| `check_changelog.py` | Fails if `CHANGELOG.md` is malformed, if a released section is edited, if an entry waiting for a release disappears, or if `mockedi/` changed without an entry. A pull request labelled `no changelog` lifts that last rule. |

## `examples/`

| File | What it is |
| --- | --- |
| `demo.sh` | A guided curl tour: an order in both dialects, the four documents back, a 997 sent back and matched, a behaviour changed and the difference shown, an order changed with an 860, trading through a directory, a broken document validated, and the dictionary served as data. It asks the mock what it has and skips what is not configured, naming the flag that enables it, so it runs against a plain `mock-edi` and shows more against one started with `--drop-dir` and a despatch window. |
| `client.py` | The same tour in Python, with no dependencies - the shape of a test that drives the mock, including building a 997 for a document the mock sent. |
| `po_bridge.py` | An example of the code the mock exists to test: middleware that reads a purchase order from SAP over OData, sends it as an 850, and posts the 855 back into SAP as an `ORDRSP` IDoc. Uses [mock-sap](https://github.com/rseufert/mock-sap) for the SAP end. |
| `test_po_bridge.py` | Integration tests for `po_bridge.py` against both mocks: a full confirmation, a short shipment, a rejected line, a partner that never answers, and an SAP outage that must not lose the 855. |

## `.github/workflows/`

| File | What it is |
| --- | --- |
| `ci.yml` | Tests on eight Python and OS combinations, the documentation and changelog checks, the documented examples, a package build and install, and the container image. |
| `publish.yml` | Releases to PyPI with Trusted Publishing (OIDC, no stored token), after checking that the tag, `pyproject.toml` and the built wheel all agree. Run manually to rehearse against TestPyPI. |
