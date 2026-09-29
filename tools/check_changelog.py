#!/usr/bin/env python3
"""Guard the changelog against drift, and against losing an entry.

The second one is why this exists. Two pull requests that each add an entry
once conflicted on the same lines, and resolving that conflict by hand is one
keystroke away from keeping one side and dropping the other. This check came to
mock-edi from its sibling project mock-sap, where exactly that happened: an
entry went missing in a merge and came back two commits later by luck rather
than by a check.

An entry waiting for a release is now a file of its own in `changelog.d/`, so
two pull requests add two files and cannot conflict at all - the conflict was
costing more than the lost entry it risked, because a pull request that
conflicts with its base runs no CI, and seven were in that state at once on
25 September. The checks below are unchanged in what they mean; they read the
new place.

Nothing can prove a resolution kept the right prose. What *can* be checked:

1. **Structure.** Every released heading has a link reference and every
   reference a heading; versions descend; `[Unreleased]` is present and its
   compare link names the newest release; `pyproject.toml` agrees with the
   newest released heading.
2. **Released sections are history.** Once a version is released its section
   is frozen: a change to it is either a mistake or a rewrite of the past.
3. **A change to the package brings an entry.** A pull request that touches
   `mockedi/` adds at least one fragment to `changelog.d/`, or moves the ones
   waiting into a new release section - which is what cutting a release does. A resolution that drops the branch's own entry leaves it with none,
   and this is what says so. Note that "touched the changelog" would not: the
   merge that lost that entry did touch it, adding a link reference and
   dropping the prose. A change that genuinely needs no entry - a comment, a rename, a
   pure refactor - carries the `no changelog` label, which lifts this rule and
   leaves the other two standing.

(2) and (3) need something to compare against, so they run only when `--base`
names a revision this checkout has; CI passes the pull request's base. Run it
directly with no arguments and you get the structural checks.

    python3 tools/check_changelog.py
    python3 tools/check_changelog.py --base origin/main

And cutting a release assembles the fragments into a dated section:

    python3 tools/check_changelog.py --release 0.6.0
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from typing import Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = "CHANGELOG.md"
PYPROJECT = "pyproject.toml"
PACKAGE = "mockedi/"
FRAGMENTS = "changelog.d"
ESCAPE_HATCH = "no changelog"

# The headings a release section carries, in the order Keep a Changelog puts
# them and the order every released section here is already written in.
KINDS = ("added", "changed", "fixed")

# `--release` cannot write the paragraph a release section opens with - the one
# saying why anyone should upgrade - so it writes this instead, and the checks
# below refuse a file that still has it. A gap only a reader would notice ships
# one day; a failing check does not. Visible rather than an HTML comment, so
# that if it ever does escape it escapes loudly.
INTRO_TODO = "RELEASE-INTRO-TODO"
INTRO_PLACEHOLDER = (
    "> %s - say why anyone should upgrade, and name anything that changes\n"
    "> behaviour. Delete these two lines; the changelog check fails while they\n"
    "> are here." % INTRO_TODO)
# `<number>.<kind>.md`, with an optional word between them: one issue can
# produce two entries of the same kind - #125 was three people's work and left
# two `added` entries in 0.5.0 - and they must not collide on a file name.
FRAGMENT = re.compile(
    r"^(?P<number>\d+|[\w.-]+?)(?:-(?P<slug>[\w.-]+))?\.(?P<kind>%s)\.md$"
    % "|".join(KINDS))

HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$")
LINK = re.compile(r"^\[([^\]]+)\]:\s*(\S+)\s*$")
BULLET = re.compile(r"^\s*[-*] ")


def _read(path: str, rev: str = "") -> str:
    """The file as it is now, or as it was at `rev`; "" when it was not there."""
    if not rev:
        with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
            return handle.read()
    try:
        return subprocess.check_output(
            ["git", "show", "%s:%s" % (rev, path)], cwd=ROOT,
            stderr=subprocess.DEVNULL).decode("utf-8")
    except subprocess.CalledProcessError:
        return ""


def sections(text):
    """The changelog as [(version, date, body)], in the order it is written."""
    out = []
    version = date = None
    body = []
    for line in text.splitlines():
        found = HEADING.match(line)
        if found:
            if version is not None:
                out.append((version, date, "\n".join(body).strip()))
            version, date = found.group(1), found.group(2)
            body = []
        elif version is not None and not LINK.match(line):
            body.append(line)
    if version is not None:
        out.append((version, date, "\n".join(body).strip()))
    return out


def bullets(body: str):
    """The entries of a section, whitespace collapsed so re-wrapping is not a change."""
    out = []
    current = ""
    for line in body.splitlines():
        if BULLET.match(line):
            if current:
                out.append(" ".join(current.split()))
            current = BULLET.sub("", line)
        elif current and line.strip():
            current += " " + line
        elif current:
            out.append(" ".join(current.split()))
            current = ""
    if current:
        out.append(" ".join(current.split()))
    return out


def _version_key(version: str):
    return tuple(int(part) for part in version.split("."))


def _changed_in_package(base: str):
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", "%s...HEAD" % base], cwd=ROOT).decode("utf-8")
    return sorted(name for name in changed.split() if name.startswith(PACKAGE))


def _resolve(base: str) -> str:
    """The base commit, fetching it first if this is a shallow or partial clone."""
    for attempt in (0, 1):
        try:
            return subprocess.check_output(
                ["git", "rev-parse", "--verify", "--quiet", base + "^{commit}"],
                cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip()
        except subprocess.CalledProcessError:
            if attempt:
                return ""
            branch = base.rsplit("/", 1)[-1]
            subprocess.call(
                ["git", "fetch", "--no-tags", "--quiet", "origin",
                 "%s:refs/remotes/origin/%s" % (branch, branch)],
                cwd=ROOT, stderr=subprocess.DEVNULL)
    return ""


def fragments(rev: str = "") -> Dict[str, str]:
    """The entries waiting for a release, by file name.

    From the working tree, or from `rev`. A name that does not match
    `<number>.<kind>.md` is reported by `check_fragments` rather than ignored
    here, so a typo in a kind cannot make an entry silently invisible.
    """
    out = {}
    if rev:
        listing = _git("ls-tree", "--name-only", "%s:%s" % (rev, FRAGMENTS))
        names = [name for name in listing.splitlines() if name]
    else:
        directory = os.path.join(ROOT, FRAGMENTS)
        names = sorted(os.listdir(directory)) if os.path.isdir(directory) else []
    for name in names:
        if name == "README.md":
            continue
        out[name] = (_git("show", "%s:%s/%s" % (rev, FRAGMENTS, name)) if rev
                     else _read("%s/%s" % (FRAGMENTS, name)))
    return out


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git"] + list(args), cwd=ROOT,
                                       stderr=subprocess.DEVNULL).decode("utf-8")
    except subprocess.CalledProcessError:
        return ""


def check_fragments(waiting: Dict[str, str]):
    """Every file in `changelog.d/` is an entry this tool can place."""
    problems = []
    for name, body in sorted(waiting.items()):
        if not FRAGMENT.match(name):
            problems.append(
                "%s/%s is not named <number>.<kind>.md with kind one of %s, so "
                "no release would know where to put it"
                % (FRAGMENTS, name, ", ".join(KINDS)))
            continue
        if not bullets(body):
            problems.append(
                "%s/%s holds no entry: a fragment is one or more `- ` bullets, "
                "written as they would appear under the heading"
                % (FRAGMENTS, name))
    return problems


def check_structure(text: str, pyproject: str):
    problems = []
    parsed = sections(text)
    versions = [v for v, _, _ in parsed]

    if not versions or versions[0] != "Unreleased":
        problems.append("the first section should be `## [Unreleased]`")
    unreleased = dict((v, b) for v, _, b in parsed).get("Unreleased", "")
    if bullets(unreleased):
        problems.append(
            "[Unreleased] holds entries; they belong in %s/ one file each, "
            "which is what stops two pull requests conflicting here"
            % FRAGMENTS)
    released = [(v, d) for v, d, _ in parsed if v != "Unreleased"]
    for version, date in released:
        if not date:
            problems.append("[%s] has no date; a released section is `## [x.y.z] - YYYY-MM-DD`" % version)
        if not re.match(r"^\d+\.\d+\.\d+$", version):
            problems.append("[%s] is not a version number" % version)

    ordered = [v for v, _ in released if re.match(r"^\d+\.\d+\.\d+$", v)]
    if ordered != sorted(ordered, key=_version_key, reverse=True):
        problems.append("released sections are not newest first: %s" % ", ".join(ordered))

    links = dict(m.groups() for m in (LINK.match(line) for line in text.splitlines()) if m)
    for version in versions:
        if version not in links:
            problems.append("[%s] has no link reference at the foot of the file" % version)
    for name in links:
        if not name.startswith("#") and name not in versions:
            problems.append("[%s] has a link reference but no section" % name)

    if ordered and "Unreleased" in links and not links["Unreleased"].endswith("v%s...HEAD" % ordered[0]):
        problems.append(
            "the [Unreleased] link compares against %s, not the newest release v%s"
            % (links["Unreleased"].rsplit("/", 1)[-1], ordered[0]))

    if INTRO_TODO in text:
        problems.append(
            "a release section still has its %s placeholder: write the "
            "paragraph saying why anyone should upgrade, and delete the two "
            "lines holding it" % INTRO_TODO)

    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    if declared and ordered and declared.group(1) != ordered[0]:
        problems.append(
            "pyproject.toml says %s but the newest released section is [%s]; a release "
            "bumps both in one commit" % (declared.group(1), ordered[0]))
    return problems


def check_against_base(text: str, before: str, base: str, labels=(),
                       waiting: Dict[str, str] = None,
                       waited: Dict[str, str] = None):
    problems = []
    now = {v: body for v, _, body in sections(text)}
    then = sections(before)
    waiting = {} if waiting is None else waiting
    waited = {} if waited is None else waited

    for version, _, body in then:
        if version == "Unreleased":
            continue
        if version not in now:
            problems.append("[%s] was released and is now missing from the file" % version)
        elif bullets(now[version]) != bullets(body):
            problems.append(
                "[%s] is already released, so its section is history; this changes it" % version)

    # Every entry that was waiting for a release is still somewhere: still
    # waiting in its fragment, or moved into the release that shipped it.
    # Section by section, because concatenating them lets one section's last
    # bullet swallow the next one's summary line and stop looking like itself.
    #
    # The entries under [Unreleased] are checked too, for the one pull request
    # that migrates them out of there into fragments - and for ever after,
    # cheaply, since there are then none to find.
    everywhere = set()
    for body in now.values():
        everywhere.update(bullets(body))
    for body in waiting.values():
        everywhere.update(bullets(body))
    was_waiting = list(bullets(dict((v, b) for v, _, b in then).get("Unreleased", "")))
    for body in waited.values():
        was_waiting.extend(bullets(body))
    for entry in was_waiting:
        if entry not in everywhere:
            problems.append("an entry waiting for a release is gone: %s"
                            % _short(entry))

    package = _changed_in_package(base)
    if package and ESCAPE_HATCH not in labels:
        added = [name for name in waiting if name not in waited
                 or waiting[name] != waited[name]]
        moved = [v for v in now if v != "Unreleased" and v not in {x for x, _, _ in then}]
        if not added and not moved:
            problems.append(
                "%s changed without an entry in %s/:\n%s\n    Say what changed "
                "and, where it is not obvious, why - it is what a user of the published "
                "package reads. If a merge resolution dropped the entry, this is it asking "
                "to come back. If the change genuinely needs none - a comment, a rename, a "
                "pure refactor - label the pull request `%s`."
                % (PACKAGE.rstrip("/"), FRAGMENTS,
                   "\n".join("      %s" % name for name in package), ESCAPE_HATCH))
    return problems


def _short(entry: str, width: int = 70) -> str:
    return entry if len(entry) <= width else entry[:width - 1] + "…"


def assemble(text: str, version: str, waiting: Dict[str, str],
             today: str = "") -> str:
    """`CHANGELOG.md` with the waiting entries as a dated release section.

    Concatenation, not formatting: a fragment holds its bullets exactly as they
    belong under the heading, so what comes out reads as though one person had
    written the section in one go. The intro is the one thing this cannot write,
    and `INTRO_PLACEHOLDER` stands in its place until somebody does.
    """
    by_kind = {kind: [] for kind in KINDS}
    for name in sorted(waiting, key=_fragment_key):
        found = FRAGMENT.match(name)
        if found is None:
            continue
        by_kind[found.group("kind")].append(waiting[name].strip("\n"))

    parts = ["## [%s] - %s" % (version, today or db_today()), "",
             INTRO_PLACEHOLDER, ""]
    for kind in KINDS:
        if not by_kind[kind]:
            continue
        parts.append("### %s" % kind.capitalize())
        parts.append("")
        for body in by_kind[kind]:
            parts.append(body)
            parts.append("")
    section = "\n".join(parts).rstrip("\n") + "\n"

    lines = text.splitlines(keepends=True)
    out, placed = [], False
    for line in lines:
        found = HEADING.match(line)
        if found and found.group(1) != "Unreleased" and not placed:
            out.append(section + "\n")
            placed = True
        out.append(line)
    if not placed:
        out.append("\n" + section)
    return _relink("".join(out), version)


def _fragment_key(name: str):
    """Fragments in issue-number order, and by name within a number.

    Deterministic rather than editorial. The order entries appear in under a
    heading is a judgement - 0.5.0 put #127 before #125 because that read
    better - and no tool can make it. Number order is the honest default, and
    reordering is part of the same by-hand pass that writes the intro.
    """
    found = FRAGMENT.match(name)
    number = found.group("number") if found else name
    return (0, int(number), name) if number.isdigit() else (1, 0, name)


def db_today() -> str:
    return datetime.date.today().isoformat()


def _relink(text: str, version: str) -> str:
    """The new version's compare link, and `[Unreleased]` re-pointed at it."""
    links = [line for line in text.splitlines() if LINK.match(line)]
    previous = ""
    for line in links:
        name = LINK.match(line).group(1)
        if re.match(r"^\d+\.\d+\.\d+$", name) and name != version:
            previous = name
            break
    base = ""
    for line in links:
        found = LINK.match(line)
        if found.group(1) == "Unreleased":
            base = found.group(2).rsplit("/compare/", 1)[0]
    out = []
    for line in text.splitlines(keepends=True):
        found = LINK.match(line)
        if found and found.group(1) == "Unreleased":
            out.append("[Unreleased]: %s/compare/v%s...HEAD\n" % (base, version))
            out.append("[%s]: %s/compare/v%s...v%s\n"
                       % (version, base, previous, version) if previous else
                       "[%s]: %s/releases/tag/v%s\n" % (version, base, version))
            continue
        out.append(line)
    return "".join(out)


def release(version: str) -> int:
    """Turn the waiting fragments into a release section, and delete them."""
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        print("%r is not a version number (x.y.z)." % version)
        return 2
    waiting = fragments()
    problems = check_fragments(waiting)
    if problems:
        print("the fragments are not ready to release:\n")
        for problem in problems:
            print("  - %s" % problem)
        return 1
    if not waiting:
        print("nothing is waiting in %s/, so there is nothing to release."
              % FRAGMENTS)
        return 1
    text = _read(CHANGELOG)
    if version in [v for v, _, _ in sections(text)]:
        print("[%s] is already a section in %s." % (version, CHANGELOG))
        return 1

    with open(os.path.join(ROOT, CHANGELOG), "w", encoding="utf-8") as handle:
        handle.write(assemble(text, version, waiting))
    for name in waiting:
        os.remove(os.path.join(ROOT, FRAGMENTS, name))

    print("[%s] assembled from %d fragment(s), which are now deleted.\n"
          % (version, len(waiting)))
    print("Two things left, both by hand:")
    print("  1. Replace the %s placeholder with the paragraph saying why\n"
          "     anyone should upgrade. The changelog check fails until you do."
          % INTRO_TODO)
    print("  2. Bump the version in %s to match." % PYPROJECT)
    return 0


MERGE_SUBJECT = re.compile(r"^Merge pull request #(\d+) from \S")


def merged_pull_request(subject: str) -> str:
    """The number of the pull request a merge commit's subject names, or "".

    A push to `main` carries no pull request in its event, so the workflow
    has no labels to pass and the entry rule fires on a change that was
    already excused by `no changelog` (#193). The merge commit says which
    pull request it came from, and this reads it.

    Deliberately narrow. Anything that is not GitHub's own merge subject -
    a hand-written commit, a revert, "Merge branch 'main'" - returns "", and
    the caller then has no labels and the check fails as it did before.
    Failing loudly is the point: a lookup that quietly passed would be worse
    than the bug it replaces.
    """
    match = MERGE_SUBJECT.match(subject.strip())
    return match.group(1) if match else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="", help="revision to compare against, e.g. origin/main")
    parser.add_argument("--labels", default="", help="comma-separated pull request labels; `%s` lifts the entry rule" % ESCAPE_HATCH)
    parser.add_argument("--release", metavar="X.Y.Z", default="",
                        help="assemble %s/ into a dated section and delete it"
                             % FRAGMENTS)
    parser.add_argument("--merged-pull-request", metavar="SUBJECT", default="",
                        help="print the pull request number a merge commit's"
                             " subject names, and nothing else")
    args = parser.parse_args()
    if args.merged_pull_request:
        found = merged_pull_request(args.merged_pull_request)
        if found:
            print(found)
        return 0
    if args.release:
        return release(args.release)
    labels = [label.strip() for label in args.labels.split(",") if label.strip()]

    text = _read(CHANGELOG)
    waiting = fragments()
    problems = check_fragments(waiting) + check_structure(text, _read(PYPROJECT))

    compared = ""
    if args.base:
        compared = _resolve(args.base)
        if not compared:
            print("note: %s is not in this checkout, so only the structure was checked." % args.base)
        else:
            problems += check_against_base(
                text, _read(CHANGELOG, compared), compared, labels,
                waiting=waiting, waited=fragments(compared))

    if problems:
        print("the changelog needs attention:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1

    print("The changelog is well formed, agrees with pyproject.toml%s; %d entr%s "
          "waiting in %s/."
          % (", and has lost nothing since %s" % args.base if compared else "",
             len(waiting), "y is" if len(waiting) == 1 else "ies are", FRAGMENTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
