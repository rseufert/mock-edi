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
4. every command-line flag is mentioned in the README

The fourth asks the real argument parser for its flags, so a flag added to
`mockedi/__main__.py` without a word in the README fails the build - fourteen
of them once existed only in `--help`.

There was a check holding the README to the number of tests the loader
discovers.  It went when the number did.  An exact count sits in one line of
prose that every branch adding a test has to edit, so it conflicted with
every other such branch - and a pull request in conflict runs no CI at all,
which made branches look stalled when they were only dirty.  "Every test
talks to a real mock over real HTTP" is the claim worth making, and it cannot
go stale.

Run it directly (`python3 tools/check_docs.py`); CI runs it on every push.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join("docs", "FILES.md")
README = "README.md"

# Files that are their own documentation, or carry nothing worth describing.
EXEMPT = {".gitignore"}

# Tokens in the index that look like a path and are therefore checked to exist.
PATH_RE = re.compile(r"`([\w./-]+\.(?:py|md|yml|yaml|toml|in|sh|cfg))`")


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, check=True,
                         stdout=subprocess.PIPE).stdout.decode()
    return sorted(line for line in out.splitlines() if line)


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def cli_flags():
    """Every long flag the command line accepts, from the parser itself."""
    sys.path.insert(0, ROOT)
    from mockedi.__main__ import build_parser
    flags = set()
    for action in build_parser()._actions:
        flags.update(o for o in action.option_strings if o.startswith("--"))
    return sorted(flags - {"--help"})


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

    # 4. every flag the command line takes is mentioned in the README
    for flag in cli_flags():
        if not re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(flag), readme):
            problems.append(
                "%s is a command-line flag the README never mentions - add it to "
                "the Configuration section" % flag)

    if problems:
        print("documentation is out of date:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1

    print("docs/FILES.md covers every tracked file, names nothing that is gone, "
          "the README layout block lists every module, and it mentions every "
          "command-line flag.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
