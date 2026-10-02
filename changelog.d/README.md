# One file per changelog entry

An entry waiting for a release lives here, not under `## [Unreleased]` in
`CHANGELOG.md`. Two pull requests that each add an entry then add two files and
cannot conflict — where before they conflicted on the same line every time, and
a pull request that conflicts with its base runs no CI at all, so whatever it
said about being green was true of an older `main`.

## Writing one

    changelog.d/<number>.<kind>.md

`<number>` is the issue the entry is about, or the pull request when there is no
issue. `<kind>` is `added`, `changed` or `fixed`, and decides which heading the
entry lands under.

The file holds the entry **exactly as it would appear under that heading** —
the leading `- `, and two spaces of indent on every line after the first:

```markdown
- **A replayed interchange is refused rather than fulfilled twice** ([#44]).
  An interchange control number a partner has used before is refused in the
  envelope's own words, and nothing behind the refused envelope is read.
```

Written that way, assembling a release is concatenation, so the section reads as
though a person had written it in one go. Anything that reformats could drift.

More than one bullet in a file is allowed: a change with two faces should not
need two files.

The reference — `[#44]` — is resolved from `CHANGELOG.md`'s link block at the
foot of the file. **Do not add the line yourself.** The release writes a
definition for every reference the new section uses, and the changelog check
fails on one that has none, so a missing link is caught without anyone editing
the foot of `CHANGELOG.md` — which is the shared line range this directory
exists to keep people out of. Asking authors to edit it was the old rule; it
brought back the conflicts `changelog.d/` had just ended, so nobody followed it
and eight references went unlinked into 0.6.0 (#226).

## Cutting a release

    python3 tools/check_changelog.py --release 0.6.0

writes the dated section into `CHANGELOG.md` with `### Added`, `### Changed` and
`### Fixed` in that order, adds the version's link reference, re-points
`[Unreleased]`, and deletes the files here.

It does not write the paragraph of prose a release section opens with, because
no tool can: that is the part saying why anyone should upgrade. The release pull
request is this command's output plus that paragraph.
