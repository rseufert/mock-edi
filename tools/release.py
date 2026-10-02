#!/usr/bin/env python3
"""Take a green `main` to a version on PyPI, in one command (#139).

A release is five things in order: the release pull request, its merge, the
tag on the merge commit, the GitHub Release, and the check that PyPI has the
files. Done by hand they can stop anywhere, and 0.4.0 did - after the merge,
so `main` said 0.4.0 for several hours while there was no tag, no release,
and PyPI's newest was 0.3.1. A rule in CONTRIBUTING.md is weaker than this.

    python3 tools/release.py 0.6.0            # from a green, clean main
    python3 tools/release.py 0.6.0 --resume   # after it stopped, anywhere

**Where it has got to is read from what exists, not remembered.** The
version `pyproject.toml` says on `origin/main`; an open pull request titled
`Release X.Y.Z`; a local `release/X.Y.Z` branch; the tag; the GitHub Release;
the files on PyPI. So `--resume` works after a crash, after somebody else
ran half of it, and in the 0.4.0 state - merged, never tagged - which no
branch name would have found, since that release came from a branch named
for something else.

**It stops, on purpose, once.** Assembling the changelog section leaves a
placeholder for the paragraph saying why anyone should upgrade, which the
changelog check fails on and only a person can write. The command stops
there, says where to write it, and `--resume` carries on.

Every wait has a limit, and says what it was waiting for when it gives up.
Standard library only; it shells out to `git`, `gh` and `python3`.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "mock-edi"
PYPI_INDEX = "https://pypi.org/simple/%s/" % PACKAGE
PUBLISH_WORKFLOW = "publish.yml"
CI_WORKFLOW = "ci.yml"

# How long each wait may take, in seconds, and how often it looks.
CHECKS_LIMIT = 45 * 60
PUBLISH_LIMIT = 30 * 60
PYPI_LIMIT = 20 * 60
POLL = 30

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)


class Stop(Exception):
    """A reason to stop, said to whoever is running the release.

    `code` 1 is a refusal or a failure; 3 is the one deliberate stop, for the
    paragraph a person has to write.
    """

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass
class Result:
    code: int
    out: str


def _run(args: Sequence[str], cwd: str = ROOT) -> Result:
    done = subprocess.run(list(args), cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True)
    return Result(done.returncode, done.stdout)


def _fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


@dataclass
class State:
    """What exists for this version, read afresh each time it is needed."""
    on_main: bool            # origin/main's pyproject.toml says this version
    pull_request: int        # an open "Release X.Y.Z" pull request, or 0
    branch: bool             # a local release/X.Y.Z branch
    tag: bool
    github_release: bool
    on_pypi: bool

    @property
    def started(self) -> bool:
        return any((self.on_main, self.pull_request, self.branch, self.tag,
                    self.github_release, self.on_pypi))

    @property
    def finished(self) -> bool:
        return self.on_main and self.tag and self.github_release and self.on_pypi


class Release:
    def __init__(self, version: str, run: Callable[..., Result] = _run,
                 fetch: Callable[[str], str] = _fetch,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 say: Callable[[str], None] = print, root: str = ROOT):
        if not VERSION_RE.match(version):
            raise Stop("%r is not a version number (x.y.z)." % version, 2)
        self.version = version
        self.tag = "v" + version
        self.branch = "release/" + version
        self.title = "Release " + version
        self.run_ = run
        self.fetch = fetch
        self.sleep = sleep
        self.clock = clock
        self.say = say
        self.root = root

    # -- the whole thing

    def release(self, resume: bool = False) -> int:
        self.git("fetch", "--quiet", "--tags", "origin")
        state = self.state()
        if state.finished:
            self.say("%s is already released: merged, tagged, on GitHub and on "
                     "PyPI. Nothing to do." % self.version)
            return 0
        if state.started and not resume:
            raise Stop("%s is part-way released (%s). Run it again with "
                       "--resume to finish it." % (self.version,
                                                   self.describe(state)))
        if not state.started and resume:
            raise Stop("nothing of %s exists yet, so there is nothing to resume; "
                       "run it without --resume." % self.version)

        if not state.on_main:
            number = state.pull_request
            if not number:
                if state.branch:
                    # Stopped after preparing - usually for the paragraph
                    # only a person can write.
                    self.finish_preparing()
                else:
                    self.preconditions()
                    self.prepare()
                number = self.propose()
            self.wait_for_checks(number)
            self.merge(number)
        commit = self.release_commit()
        if not self.state().tag:
            self.make_tag(commit)
        if not self.state().github_release:
            self.make_github_release(commit)
        self.wait_for_publish()
        self.wait_for_pypi()
        self.say("%s is released: tagged %s, on GitHub, and listed on PyPI."
                 % (self.version, self.tag))
        return 0

    def state(self) -> State:
        main_version = self.version_at("origin/main")
        return State(
            on_main=main_version == self.version,
            pull_request=self.open_pull_request(),
            # Locally, or pushed by a run on another machine that stopped
            # before opening the pull request.
            branch=(self.git("rev-parse", "--verify", "--quiet",
                             "refs/heads/" + self.branch, check=False).code == 0
                    or bool(self.git("ls-remote", "--heads", "origin",
                                     self.branch, check=False).out.strip())),
            tag=bool(self.git("ls-remote", "--tags", "origin",
                              "refs/tags/" + self.tag).out.strip()),
            github_release=self.gh("release", "view", self.tag, "--json",
                                   "tagName", check=False).code == 0,
            on_pypi=self.listed_on_pypi())

    @staticmethod
    def describe(state: State) -> str:
        have = [name for name, present in (
            ("merged to main", state.on_main),
            ("pull request #%d open" % state.pull_request, state.pull_request),
            ("release branch", state.branch), ("tag", state.tag),
            ("GitHub Release", state.github_release), ("on PyPI", state.on_pypi))
            if present]
        return ", ".join(have) or "nothing"

    # -- 1. refuse unless main is fit to release

    def preconditions(self) -> None:
        if self.git("status", "--porcelain").out.strip():
            raise Stop("the working tree has uncommitted changes; a release is "
                       "made from a clean checkout of main.")
        if self.git("rev-parse", "--abbrev-ref", "HEAD").out.strip() != "main":
            raise Stop("this checkout is not on main.")
        head = self.git("rev-parse", "HEAD").out.strip()
        if head != self.git("rev-parse", "origin/main").out.strip():
            raise Stop("main is not level with origin/main; pull or push first.")
        runs = self.gh("run", "list", "--workflow", CI_WORKFLOW, "--branch",
                       "main", "--commit", head, "--json", "status,conclusion",
                       "-q", '.[]|.status+" "+.conclusion').out.split("\n")
        runs = [line.strip() for line in runs if line.strip()]
        if not runs or any(line != "completed success" for line in runs):
            raise Stop("CI is not green on main's head %s (%s); release from a "
                       "commit whose checks have passed."
                       % (head[:7], ", ".join(runs) or "no run"))
        # Every open pull request with its labels, and the labelled ones
        # picked out here. `--label` would be shorter, and is answered from
        # GitHub's search index, which is behind a label put on a moment ago
        # (#190) - the one this check exists to catch.
        waiting = self.gh("pr", "list", "--state", "open", "--limit", "500",
                          "--json", "number,labels", "-q",
                          '.[]|select(any(.labels[]; .name=="%s"))|.number'
                          % self.version).out.split()
        if waiting:
            raise Stop("pull request%s %s %s labelled %s and still open; merge "
                       "or relabel before releasing."
                       % ("s" if len(waiting) > 1 else "",
                          ", ".join("#" + n for n in waiting),
                          "are" if len(waiting) > 1 else "is", self.version))

    # -- 2. the release commit: version, changelog section, the checks

    def prepare(self) -> None:
        self.git("switch", "--quiet", "-c", self.branch, "origin/main")
        self.python("tools/check_changelog.py", "--release", self.version)
        self.bump()
        self.say("assembled the %s section and set pyproject.toml to %s."
                 % (self.version, self.version))
        self.finish_preparing()

    def finish_preparing(self) -> None:
        """The part of preparing that is repeated on --resume after the stop."""
        self.git("switch", "--quiet", self.branch)
        if self.python("tools/check_changelog.py", check=False).code != 0:
            raise Stop(
                "CHANGELOG.md's %s section still needs the paragraph saying why "
                "anyone should upgrade, in place of the placeholder - the one "
                "part a person has to write. Write it on branch %s, then run "
                "this again with --resume." % (self.version, self.branch), 3)
        self.python("tools/check_docs.py")
        self.say("running the suite; this takes a few minutes.")
        self.python("-m", "unittest", "discover", "-s", "tests")
        self.git("add", "-A")
        if self.git("diff", "--cached", "--quiet", check=False).code != 0:
            self.git("commit", "--quiet", "-m", self.title)

    def bump(self) -> None:
        path = os.path.join(self.root, "pyproject.toml")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        if not PYPROJECT_VERSION_RE.search(text):
            raise Stop("pyproject.toml has no version line to bump.")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(PYPROJECT_VERSION_RE.sub(
                'version = "%s"' % self.version, text, count=1))

    # -- 3. the pull request, its checks, its merge

    def propose(self) -> int:
        self.git("push", "--quiet", "-u", "origin", self.branch)
        body = ("The %s release: the version in `pyproject.toml` and the "
                "changelog section assembled from `changelog.d/`. Opened by "
                "`tools/release.py`, which merges it once its checks pass and "
                "then tags, releases and checks PyPI." % self.version)
        self.gh("pr", "create", "--base", "main", "--head", self.branch,
                "--title", self.title, "--body", body)
        number = self.open_pull_request()
        if not number:
            raise Stop("the release pull request was created but cannot be "
                       "found as open; look for %r." % self.title)
        self.say("opened #%d." % number)
        return number

    def wait_for_checks(self, number: int) -> None:
        def looked() -> Optional[bool]:
            buckets = self.gh("pr", "checks", str(number), "--json", "bucket",
                              "-q", ".[].bucket", check=False).out.split()
            if not buckets or "pending" in buckets:
                return None
            bad = [b for b in buckets if b not in ("pass", "skipping")]
            if bad:
                raise Stop("#%d's checks did not pass (%s); fix them, then run "
                           "this again with --resume." % (number, ", ".join(bad)))
            return True
        self.wait("#%d's checks to pass" % number, CHECKS_LIMIT, looked)

    def merge(self, number: int) -> None:
        self.gh("pr", "merge", str(number), "--merge", "--delete-branch")
        self.git("fetch", "--quiet", "--tags", "origin")
        self.say("merged #%d." % number)

    # -- 4. the tag and the GitHub Release

    def release_commit(self) -> str:
        """The commit on main that first said this version: the merge to tag.

        Found from history rather than from a pull request, so the 0.4.0 case
        - merged from a branch named for something else - is found too.
        """
        found = self.git("log", "--first-parent", "--reverse", "--format=%H",
                         "-S", 'version = "%s"' % self.version, "origin/main",
                         "--", "pyproject.toml").out.split()
        if not found:
            raise Stop("no commit on origin/main sets pyproject.toml to %s."
                       % self.version)
        return found[0]

    def make_tag(self, commit: str) -> None:
        self.git("tag", "-a", self.tag, commit, "-m", "%s %s"
                 % (PACKAGE, self.version))
        self.git("push", "--quiet", "origin", self.tag)
        self.say("tagged %s at %s." % (self.tag, commit[:7]))

    def make_github_release(self, commit: str) -> None:
        notes = section(self.git("show", "%s:CHANGELOG.md" % commit).out,
                        self.version)
        if not notes:
            raise Stop("CHANGELOG.md at %s has no %s section to use as the "
                       "release notes." % (commit[:7], self.version))
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(notes)
        try:
            self.gh("release", "create", self.tag, "--title", "%s %s"
                    % (PACKAGE, self.version), "--notes-file", handle.name,
                    "--verify-tag")
        finally:
            os.remove(handle.name)
        self.say("published the GitHub Release %s." % self.tag)

    # -- 5. the Publish workflow, and PyPI itself

    def wait_for_publish(self) -> None:
        def looked() -> Optional[bool]:
            runs = self.gh("run", "list", "--workflow", PUBLISH_WORKFLOW,
                           "--event", "release", "--json",
                           "headBranch,status,conclusion", "-q",
                           '.[]|select(.headBranch=="%s")|.status+" "+.conclusion'
                           % self.tag, check=False).out.split("\n")
            runs = [line.strip() for line in runs if line.strip()]
            if not runs or not runs[0].startswith("completed"):
                return None
            if runs[0] != "completed success":
                raise Stop("the Publish workflow for %s finished %s; look at it, "
                           "fix it, and re-run it from the Actions tab - then "
                           "--resume confirms PyPI." % (self.tag, runs[0]))
            return True
        self.wait("the Publish workflow for %s" % self.tag, PUBLISH_LIMIT, looked)

    def wait_for_pypi(self) -> None:
        self.wait("PyPI to list %s %s at %s" % (PACKAGE, self.version, PYPI_INDEX),
                  PYPI_LIMIT, lambda: True if self.listed_on_pypi() else None)

    def listed_on_pypi(self) -> bool:
        """Asked of PyPI's index directly, not of pip, whose cache lies."""
        try:
            page = self.fetch(PYPI_INDEX)
        except Exception:        # unreachable is "not yet", and the wait says so
            return False
        # The version and then what ends it - the wheel's "-py3-..." or the
        # sdist's ".tar.gz" - never a prefix: 0.1.1 is not listed because
        # 0.1.10 is, and a backport released after a later patch is exactly
        # when that would pass for done.
        names = {PACKAGE, PACKAGE.replace("-", "_")}
        return any(re.search(r"%s-%s(?:-|\.tar|\.zip)"
                             % (re.escape(name), re.escape(self.version)), page)
                   for name in names)

    # -- plumbing

    def wait(self, what: str, limit: float, looked: Callable[[], Optional[bool]]):
        started = self.clock()
        while True:
            if looked():
                return
            if self.clock() - started >= limit:
                raise Stop("gave up waiting for %s after %d minutes; run this "
                           "again with --resume once it has happened."
                           % (what, limit // 60))
            self.sleep(POLL)

    def open_pull_request(self) -> int:
        """The open release pull request's number, or 0.

        Found by its branch. Asking by title goes through GitHub's search
        index, which lags a new pull request by seconds to minutes, so the
        ask straight after creating one was the ask most likely to miss it
        (#190). A branch's pull requests are listed from the repository.
        """
        found = self.gh("pr", "list", "--state", "open", "--head", self.branch,
                        "--json", "number", "-q", ".[].number",
                        check=False).out.split()
        return int(found[0]) if found else 0

    def version_at(self, revision: str) -> str:
        found = PYPROJECT_VERSION_RE.search(
            self.git("show", "%s:pyproject.toml" % revision, check=False).out)
        return found.group(1) if found else ""

    def git(self, *args: str, check: bool = True) -> Result:
        return self._call(("git",) + args, check)

    def gh(self, *args: str, check: bool = True) -> Result:
        return self._call(("gh",) + args, check)

    def python(self, *args: str, check: bool = True) -> Result:
        return self._call((sys.executable,) + args, check)

    def _call(self, args: Tuple[str, ...], check: bool) -> Result:
        result = self.run_(args)
        if check and result.code != 0:
            raise Stop("`%s` failed:\n%s" % (" ".join(args), result.out.strip()))
        return result


def section(changelog: str, version: str) -> str:
    """A released section's body, without its heading: the release notes."""
    lines = changelog.splitlines()
    heading = re.compile(r"^## \[%s\]" % re.escape(version))
    out: List[str] = []
    inside = False
    for line in lines:
        if heading.match(line):
            inside = True
            continue
        if inside and (line.startswith("## [") or re.match(r"^\[[^\]]+\]: ", line)):
            break
        if inside:
            out.append(line)
    return "\n".join(out).strip() + "\n" if inside else ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="the version to release, x.y.z")
    parser.add_argument("--resume", action="store_true",
                        help="finish a release that stopped part-way")
    args = parser.parse_args(argv)
    try:
        return Release(args.version).release(resume=args.resume)
    except Stop as stop:
        print("release %s stopped: %s" % (args.version, stop), file=sys.stderr)
        return stop.code


if __name__ == "__main__":
    sys.exit(main())
