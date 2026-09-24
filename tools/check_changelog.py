#!/usr/bin/env python3
"""Guard CHANGELOG.md against drift, and against losing an entry.

The second one is why this exists. Two pull requests that each add a bullet
under `## [Unreleased]` conflict on the same lines, and resolving that conflict
by hand is one keystroke away from keeping one side and dropping the other.
This check came to mock-edi from its sibling project mock-sap, where exactly
that happened: an entry went missing in a merge and came back two commits
later by luck rather than by a check.

Nothing can prove a resolution kept the right prose. What *can* be checked:

1. **Structure.** Every released heading has a link reference and every
   reference a heading; versions descend; `[Unreleased]` is present and its
   compare link names the newest release; `pyproject.toml` agrees with the
   newest released heading.
2. **Released sections are history.** Once a version is released its section
   is frozen: a change to it is either a mistake or a rewrite of the past.
3. **A change to the package brings an entry.** A pull request that touches
   `mockedi/` adds at least one bullet under `## [Unreleased]`, or moves the
   existing ones into a new release section - which is what cutting a release
   does. A resolution that drops the branch's own entry leaves it with none,
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
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = "CHANGELOG.md"
PYPROJECT = "pyproject.toml"
PACKAGE = "mockedi/"
ESCAPE_HATCH = "no changelog"

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


def check_structure(text: str, pyproject: str):
    problems = []
    parsed = sections(text)
    versions = [v for v, _, _ in parsed]

    if not versions or versions[0] != "Unreleased":
        problems.append("the first section should be `## [Unreleased]`")
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

    declared = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    if declared and ordered and declared.group(1) != ordered[0]:
        problems.append(
            "pyproject.toml says %s but the newest released section is [%s]; a release "
            "bumps both in one commit" % (declared.group(1), ordered[0]))
    return problems


def check_against_base(text: str, before: str, base: str, labels=()):
    problems = []
    now = {v: body for v, _, body in sections(text)}
    then = sections(before)

    for version, _, body in then:
        if version == "Unreleased":
            continue
        if version not in now:
            problems.append("[%s] was released and is now missing from the file" % version)
        elif bullets(now[version]) != bullets(body):
            problems.append(
                "[%s] is already released, so its section is history; this changes it" % version)

    # Every entry that was waiting for a release is still somewhere: still
    # waiting, or moved into the release that shipped it. Section by section,
    # because concatenating them lets one section's last bullet swallow the
    # next one's summary line and stop looking like itself.
    everywhere = set()
    for body in now.values():
        everywhere.update(bullets(body))
    for entry in bullets(dict((v, b) for v, _, b in then).get("Unreleased", "")):
        if entry not in everywhere:
            problems.append("an entry under [Unreleased] is gone: %s" % _short(entry))

    package = _changed_in_package(base)
    if package and ESCAPE_HATCH not in labels:
        added = [e for e in bullets(now.get("Unreleased", "")) if e not in bullets(dict((v, b) for v, _, b in then).get("Unreleased", ""))]
        moved = [v for v in now if v != "Unreleased" and v not in {x for x, _, _ in then}]
        if not added and not moved:
            problems.append(
                "%s changed without an entry under [Unreleased]:\n%s\n    Say what changed "
                "and, where it is not obvious, why - it is what a user of the published "
                "package reads. If a merge resolution dropped the entry, this is it asking "
                "to come back. If the change genuinely needs none - a comment, a rename, a "
                "pure refactor - label the pull request `%s`."
                % (PACKAGE.rstrip("/"), "\n".join("      %s" % name for name in package), ESCAPE_HATCH))
    return problems


def _short(entry: str, width: int = 70) -> str:
    return entry if len(entry) <= width else entry[:width - 1] + "…"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="", help="revision to compare against, e.g. origin/main")
    parser.add_argument("--labels", default="", help="comma-separated pull request labels; `%s` lifts the entry rule" % ESCAPE_HATCH)
    args = parser.parse_args()
    labels = [label.strip() for label in args.labels.split(",") if label.strip()]

    text = _read(CHANGELOG)
    problems = check_structure(text, _read(PYPROJECT))

    compared = ""
    if args.base:
        compared = _resolve(args.base)
        if not compared:
            print("note: %s is not in this checkout, so only the structure was checked." % args.base)
        else:
            problems += check_against_base(text, _read(CHANGELOG, compared), compared, labels)

    if problems:
        print("CHANGELOG.md needs attention:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1

    print("CHANGELOG.md is well formed, agrees with pyproject.toml%s."
          % (", and has lost nothing since %s" % args.base if compared else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
