#!/usr/bin/env python3
"""Guard docs/FILES.md against drift.

This checks *coverage*, not prose: it cannot tell whether a description is
still true, only whether a file exists that nobody documented, or a file is
documented that no longer exists.  That catches the common failure - a module
added without a line in the index - and leaves the judgement calls to review.

Five checks:

1. every tracked file is named in docs/FILES.md
2. every file named in docs/FILES.md exists
3. every module of the package appears in the README's layout block
4. every command-line flag is mentioned in the README
5. every partner behaviour has a row in the README's behaviour table, and
   the row names the partner roles it applies to

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

# Directories whose contents are transient rather than part of the repository's
# furniture. `changelog.d/` holds one file per entry waiting for a release, and
# they come and go with every pull request: a row each would put this index in
# the way of exactly the pull requests #138 freed from CHANGELOG.md, and move
# the conflict here instead of removing it. Its README is documented.
EXEMPT_DIRECTORIES = ("changelog.d/",)

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


def behaviours():
    """Every partner behaviour and the roles it applies to, from the mock's own table."""
    sys.path.insert(0, ROOT)
    from mockedi.db import BEHAVIOUR_ROLES, BEHAVIOURS
    return [(name, BEHAVIOUR_ROLES[name]) for name in sorted(BEHAVIOURS)]


def main():
    index = read(INDEX)
    readme = read(README)
    problems = []

    # 1. undocumented files
    for path in tracked_files():
        name = os.path.basename(path)
        if name in EXEMPT or path == INDEX:
            continue
        if (path.startswith(EXEMPT_DIRECTORIES) and name != "README.md"):
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
        if token.startswith(EXEMPT_DIRECTORIES):
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

    # 5. every behaviour a partner can be set to has a row in the README,
    #    which says which partner roles it applies to
    for name, roles in behaviours():
        row = re.search(r"^\| `%s` \|([^|]*)\|" % re.escape(name), readme, re.M)
        if row is None:
            problems.append(
                "the behaviour %r has no row in the README's behaviour table"
                % name)
            continue
        said = sorted(re.findall(r"\b(customer|supplier)\b", row.group(1)))
        if said != sorted(roles):
            problems.append(
                "the README says the behaviour %r is for %s; db.BEHAVIOUR_ROLES "
                "says %s" % (name, " and ".join(said) or "no one",
                             " and ".join(roles)))

    if problems:
        print("documentation is out of date:\n")
        for problem in problems:
            print("  - %s" % problem)
        print("\n%d problem(s)." % len(problems))
        return 1

    print("docs/FILES.md covers every tracked file, names nothing that is gone, "
          "the README layout block lists every module, it mentions every "
          "command-line flag, and it has a row for every behaviour, naming "
          "the roles it applies to.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
