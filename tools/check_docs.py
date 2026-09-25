#!/usr/bin/env python3
"""Guard docs/FILES.md against drift.

This checks *coverage*, not prose: it cannot tell whether a description is
still true, only whether a file exists that nobody documented, or a file is
documented that no longer exists.  That catches the common failure - a module
added without a line in the index - and leaves the judgement calls to review.

Four checks:

1. every tracked file is named in docs/FILES.md
2. every file named in docs/FILES.md exists
3. every module of the package appears in the README's layout block
4. the test count the README quotes is the number unittest discovers

The fourth is the one number in the prose that can be checked mechanically,
and it had drifted by eighty before anyone noticed.  It is counted by asking
the loader rather than by grepping for `def test_`, so a test method that
arrives through a base class is counted the way running the suite counts it.

Run it directly (`python3 tools/check_docs.py`); CI runs it on every push.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join("docs", "FILES.md")
README = "README.md"

# Files that are their own documentation, or carry nothing worth describing.
EXEMPT = {".gitignore"}

# The sentence in the README's Tests section that quotes a count.
COUNT_RE = re.compile(r"\b(\d+) tests, every one of them\b")

# Tokens in the index that look like a path and are therefore checked to exist.
PATH_RE = re.compile(r"`([\w./-]+\.(?:py|md|yml|yaml|toml|in|sh|cfg))`")


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True,
                         stdout=subprocess.PIPE).stdout.decode()
    return sorted(line for line in out.splitlines() if line)


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def count_tests():
    """How many tests the suite has, counted the way running it counts them."""
    suite = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"))
    if unittest.defaultTestLoader.errors:
        raise SystemExit("the test suite does not import:\n\n%s"
                         % "\n".join(unittest.defaultTestLoader.errors))

    def walk(item):
        if isinstance(item, unittest.TestSuite):
            return sum(walk(child) for child in item)
        return 1

    return walk(suite)


def main():
    index = read(INDEX)
    readme = read(README)
    problems = []

    # 1. undocumented files
    for path in tracked_files():
        name = os.path.basename(path)
        if name in EXEMPT or path == INDEX:
            continue
        if ("`%s`" % path) not in index and ("`%s`" % name) not in index:
            problems.append(
                "%s is not documented in %s - add a row describing it" % (path, INDEX))

    # 2. documented files that no longer exist
    existing = set(tracked_files())
    basenames = {os.path.basename(p) for p in existing}
    for token in sorted(set(PATH_RE.findall(index))):
        if token in existing or token in basenames:
            continue
        problems.append(
            "%s mentions `%s`, which no longer exists - update or remove the row"
            % (INDEX, token))

    # 3. the README layout block must list every module of the package
    layout = re.search(r"## Layout\n+```\n(.*?)```", readme, re.S)
    if layout is None:
        problems.append("could not find the layout block in %s" % README)
    else:
        for path in existing:
            if path.startswith("mockedi/") and path.endswith(".py"):
                base = os.path.basename(path)
                if base.startswith("__"):
                    continue  # dunder modules are not part of the map
                if path not in layout.group(1):
                    problems.append(
                        "%s is missing from the layout block in %s" % (path, README))

    # 4. the test count quoted in the README
    quoted = COUNT_RE.search(readme)
    if quoted is None:
        problems.append(
            "could not find the sentence in %s that quotes a test count - if the "
            "wording changed, update COUNT_RE" % README)
    else:
        discovered = count_tests()
        if int(quoted.group(1)) != discovered:
            problems.append(
                "%s says %s tests, the suite has %d - say %d"
                % (README, quoted.group(1), discovered, discovered))

    if problems:
        print("documentation is out of date:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1

    print("docs/FILES.md covers every tracked file, names nothing that is gone, "
          "the README layout block lists every module, and it quotes the right "
          "test count.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
