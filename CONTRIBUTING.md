# Contributing

Thanks for looking. mock-edi is a mock trading partner: it speaks the wire
shapes of ASC X12 and UN/EDIFACT over AS2 so that EDI integrations can be built
and tested without a counterparty. Everything below is about keeping it useful
for that.

## What the project values

These are not style preferences; they decide what gets merged.

**Fidelity over convenience.** If a real partner behaves a certain way, the
mock behaves that way - even when the real behaviour is inconvenient. ISA is
padded to its fixed widths and its delimiters are read back out of it rather
than assumed. `TDS` carries an integer with two implied decimals. A rejected
line commits to no delivery date. A mock that accepts what a real translator
rejects teaches a client a lie it will discover in production.

**Say when you are guessing.** Where trading partners genuinely differ, the
mock picks one profile and says so out loud rather than implying authority.
Line-level status in an `ORDRSP` is the clearest case: the confirmed quantity
goes in `QTY+113` with the shortfall in `QTY+83` and the reason in `FTX+AAO`,
and both the code and the README state that this is a choice. Inventing a
segment or a code that the standards do not have costs more than leaving a gap.

**No dependencies.** The Python standard library and SQLite, nothing else. A
mock you cannot install in a locked-down CI image is a mock nobody runs. This
is not negotiable, and it is why S/MIME is refused rather than half-built.

**Declare, do not hand-write.** Segments, elements, loops, code lists and
transaction sets are declarations in `mockedi/schema.py`; parsing, validation,
generation and the published dictionary are derived from them. If you find
yourself writing the same shape in two places, the declaration is missing.

**Refuse rather than half-implement.** An encrypted AS2 payload, an unknown
transaction set and an interchange from an unregistered sender all produce an
answer that names what *is* supported. Silently ignoring something is the one
thing worse than not having it.

**The wire is the product.** Behaviour a client cannot observe does not need to
exist; behaviour it can observe needs to be right. Control numbers come from
number ranges and shipment ids are derived so they are reproducible, because a
client sees those. No warehouse is simulated, because no client can tell.

## Getting set up

Nothing to install:

```bash
git clone https://github.com/rseufert/mock-edi
cd mock-edi
python3 -m mockedi --port 8080        # it is already runnable
python3 -m unittest discover -s tests -v
python3 tools/check_docs.py
python3 tools/check_changelog.py
```

Python 3.8 or newer. There is no build step, no virtualenv to create and
nothing to compile.

## Where things live

| Adding this | Goes here | Notes |
| --- | --- | --- |
| A segment or element | `mockedi/schema.py` | Define it once and reference it; `N1`, `DTM` and `RFF` are shared across sets |
| A code value | `mockedi/schema.py` | The code lists are what make validation mean something - a list that accepts everything acknowledges everything |
| A difference between versions | `mockedi/schema.py` `REVISIONS` | Declare the segment as the later version has it and list it under that version; sets are declared once, at the first version, and derived for the others |
| A transaction set | `mockedi/schema.py`, then `transactions.py` | The definition first, then a reader or a writer, then a row in `SET_FOR_KIND` |
| A kind of guide narrowing | `mockedi/profiles.py` (`check` and `_narrow_children`) | It must only narrow: `check` refuses anything that would let through what the dictionary refuses |
| A partner behaviour | `mockedi/documents.py` (`decide`) and `db.BEHAVIOURS` | Keep the precedence rules in the docstring true |
| A validation check | `mockedi/validate.py` | Produce a finding, not a sentence: it has to render as both a 997 and a CONTRL |
| An endpoint | `mockedi/server.py` | Add it to the index page and the README table too |
| A CLI flag | `mockedi/__main__.py` and `server.Config` | |
| A table, column or index | `mockedi/db.py` `SCHEMA` / `INDEXES` | Bump `SCHEMA_VERSION`; `tests/test_upgrade.py` fails until you do. Additions upgrade old files by themselves; a rename or retype needs a step in `db.upgrade` |

If a change touches more than one of these, it is usually two changes.

## What a good pull request looks like

- **A test that goes over HTTP.** Every test in `tests/` drives a real mock on
  a real socket; nothing is stubbed. Put it in the module for the surface you
  touched, or add one and give it a row in `docs/FILES.md`.
- **Assertions that could fail.** Check a total against the lines it
  summarises, not against itself. The nastiest bug in this project's short
  history - a loop walker that treated a repeated `N1` as a second use -
  produced plausible-looking 997s and was caught only by validating the mock's
  own output against its own dictionary.
- **Documentation that keeps up.** `tools/check_docs.py` fails the build if a
  tracked file has no row in `docs/FILES.md`, if a row names a file that is
  gone, if a module is missing from the README's layout block, if a
  command-line flag has no mention in the README's Configuration section, or
  if a behaviour has no row in the README's table. It checks coverage, not prose - keeping the prose
  true is on you.
- **A changelog entry, as a file of its own.** A pull request that touches
  `mockedi/` adds `changelog.d/<number>.<kind>.md`, where kind is `added`,
  `changed` or `fixed`, holding the bullet exactly as it will appear under that
  heading - see [`changelog.d/README.md`](changelog.d/README.md). One file
  each, because two pull requests editing `## [Unreleased]` conflicted on the
  same line every time, and a pull request that conflicts with its base runs no
  CI at all: seven were in that state at once on 25 September, each one's green
  true only of an older `main`.

  `tools/check_changelog.py` fails a pull request with no fragment - an entry,
  not merely a changed file. It is what a user of the published package reads.
  The same check holds released sections to being history and refuses to let an
  entry waiting for a release disappear. A change that genuinely needs no entry
  - a comment, a rename, a pure refactor - can carry the `no changelog` label,
  which lifts that one rule and leaves the others standing. Only someone with
  triage rights can label a pull request, so say in the description why no
  entry is needed and a maintainer will apply it.
- **No new dependencies.** See above.
- **A commit message that says what changed and why.** The why is the part a
  reader cannot reconstruct. Wrap at 72 characters.

Small, focused pull requests are easier to take than large ones. If you are
unsure whether something fits, open an issue first and say what you are trying
to test against the mock - that is usually the fastest way to the right shape.

## How the team works

Several people work on this at once, most of them through one GitHub account,
so the usual machinery - assignees, approvals - is not available and the
agreement has to be written down instead. Each rule is here because its
absence cost something, and says what.

**Claim an issue before you start.** Comment on it: that you are taking it,
and your approach in a few lines - what you will change, in which files, and
what you will leave alone. Add the `claimed` label. An issue with no claim is
free; one with a claim is not, until its owner says so. Two people once built
the same issue in the same seven files within minutes of each other, because
a label assigned a queue and nobody had said "mine".

**One issue, one pull request, and split rather than grow.** If the work turns
out to be two things, open the second issue. A follow-up pull request that
only adds tests is welcome.

**Decisions live on the issue.** Whoever is closest to the code may overrule
the issue's text - an issue is written before the code is read closely - but
says so on the issue, with the reason, before anything else depends on it. A
session that restarts can then pick up the thread from the tracker rather
than from a conversation it no longer has.

**Numbers that are shared are assigned on the issue.** The schema version is
the usual one: if two pieces of work each migrate the database, the issue says
which takes which number before either writes a migration.

**Some changes need a second reader**, someone other than the author:

| The pull request | Is read by |
| --- | --- |
| comes from the junior | the senior |
| changes what goes on the wire - a writer, the dictionary in `schema.py`, `ack.py`, `envelope.py`, `x12.py`, `edifact.py` | another coder |
| changes the database schema or a migration in `db.py` | another coder |

Approvals cannot be recorded when everyone is one account, so a review is a
comment on the pull request that begins `Review:` and says what was read, what
was run, and what was found. A reviewer reproduces what they report. An
author shows, in the description, that each new test fails without the
change. Everything else may merge on green alone - which is fast, and is why
these two areas are singled out: a wrong byte on the wire and a wrong
migration are the mistakes a test suite written by the same author does not
catch. The project's own best test validates the mock's output against its
own dictionary, and a wrong dictionary passed it.

**Merging.** A pull request merges when it is mergeable, every check has
passed, and its second reader, if it needs one, has commented. Its author
merges it; the project manager does when the author cannot. **Merge commits
only** - `gh pr merge --merge` - and squash and rebase merging are switched
off on the repository. After a squash or a rebase, git cannot tell a merged
branch from an unmerged one: a merged branch was once reopened as a duplicate
pull request for exactly that reason. The branch is deleted when it merges.

**A conflict belongs to the author.** Nobody pushes to a branch someone else
is working on; if the resolution is not obvious, post the steps on the pull
request. A pull request that conflicts with `main` runs no CI at all, so
whatever it says about being green was true of an older `main`. And once a
review has started, merge `main` in rather than rebasing, so the review's
comments still point at something.

**One worktree per person.** Never switch the branch of a checkout somebody
else may be using:

```bash
git worktree add ../mock-edi-125 -b feat/partner-role origin/main
```

**Leave nothing running.** A loop that polls for a CI run outlives the session
that started it. Give it a limit, or stop it when you are done.

## Reporting a missing or wrong shape

The most useful bug report contains the interchange a real trading partner
sent, with anything sensitive removed, beside what the mock produced. Segment
tags, element positions, the exact envelope and the version all matter. If you
cannot share a document, the transaction set and a description of the
difference is still plenty to work with.

`POST /_mock/validate` is often the fastest way to show one: it returns the
mock's reading of a document as prose, without changing anything.

## Releasing (maintainers)

**A release assembles the fragments.** `python3 tools/check_changelog.py
--release X.Y.Z` writes the dated section into `CHANGELOG.md` with its headings
in order and its links, and deletes the files it came from. Two things are left
by hand, and both are meant to be: the paragraph saying why anyone should
upgrade - the tool writes a placeholder the changelog check *fails* on, so a
release cannot reach `main` without one, and cannot reach it with an invented
summary - and the version in `pyproject.toml`, which stays a deliberate edit
because the check that the two agree is only worth having while one of them is.
The order entries appear in under a heading is editorial too; reorder them in
the same pass.

**A release is one step, taken by one person.** The release pull request,
the tag, the GitHub Release and the check that PyPI has it follow each other
without a pause. If you cannot finish, do not merge the release pull request:
once it merges, `main` says a version exists, and until the tag is pushed
nothing else does. That gap was once several hours long.

`pyproject.toml` is the only place the version is written; `mockedi.__version__`
reads it back from the installed package metadata.

```bash
# bump `version` in pyproject.toml, commit, then:
git tag v0.2.0 && git push origin v0.2.0
gh release create v0.2.0 --generate-notes     # or write the notes by hand
```

Publishing the GitHub Release runs the tests, builds the distributions, checks
that the tag, `pyproject.toml` and the built wheel agree, and uploads to PyPI
through [Trusted Publishing](https://docs.pypi.org/trusted-publishers/) - there
is no API token anywhere. Running the `Publish` workflow by hand publishes to
TestPyPI instead. Add the release to [`CHANGELOG.md`](CHANGELOG.md) in the same
commit as the version bump.

Verify the release by installing the exact version into a clean environment:

```bash
pip install --no-cache-dir "mock-edi==0.2.0"
```

Both parts of that matter, and they fix different problems that look the same
while you are watching them.

**Pin the version.** PyPI's index propagates per edge node, so an install
immediately after a release can still be served the previous one. A plain
`pip install --upgrade` will take it and report success.

**Pass `--no-cache-dir`.** pip caches the index page it fetched earlier, so a
virtualenv that has installed this package before - which is exactly the one
you reach for to verify a release - keeps being told the new version does not
exist:

```
ERROR: Could not find a version that satisfies the requirement mock-edi==0.2.0
       (from versions: 0.1.0)
```

That is not propagation, and waiting will not fix it. `--no-cache-dir` forces
a fresh index fetch, not merely a fresh download.

To tell the two apart, ask PyPI directly:

```bash
curl -s https://pypi.org/simple/mock-edi/ | grep 0.2.0
```

If the files are listed and pip still disagrees, it is pip's cache. If they
are not listed yet, it is propagation, and waiting is the right move.

## Licence

By contributing you agree that your work is licensed under the
[MIT Licence](LICENSE), the same terms as the rest of the project.
