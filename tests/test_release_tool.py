"""`tools/release.py`: a release in one command, which cannot stop halfway (#139).

Driven against a fake world: `git`, `gh` and PyPI answered from a small model
of the repository, GitHub and the index, and a clock that only moves when the
tool sleeps. Nothing here touches a real remote. The case that matters most is
0.4.0's - merged, never tagged - which `--resume` has to finish from what
exists alone.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import release as tool

VERSION = "0.6.0"
CHANGELOG = """# Changelog

## [Unreleased]

## [0.6.0] - 2026-09-28

Why anyone should upgrade.

### Added

- A thing ([#1]).

## [0.5.0] - 2026-09-27

Older.

[#1]: https://example.test/1
"""


class World:
    """The repository, GitHub and PyPI, as far as the tool can see them."""

    def __init__(self, **state):
        self.main_version = "0.5.0"
        self.open_pr = 0
        self.branch = False
        self.remote_branch = False
        self.tag = False
        self.gh_release = False
        self.pypi = False
        self.dirty = False
        self.on = "main"
        self.head, self.origin = "h3ad", "h3ad"
        self.ci = ["completed success"]
        self.labelled = []
        self.intro_written = True
        self.checks = [["pending"], ["pass", "pass", "skipping"]]
        self.publish = [[], ["in_progress "], ["completed success"]]
        self.pypi_after_publish = True
        self.merge_sha = "m3rg3d"
        self.calls = []
        self.tagged_at = None
        self.notes = None
        self.__dict__.update(state)

    # -- the tool's run(), fetch(), sleep() and clock()

    def run(self, args):
        self.calls.append(" ".join(args))
        a = list(args)
        ok = tool.Result(0, "")
        if a[0] == "git":
            return self.git(a[1:])
        if a[0] == "gh":
            return self.gh(a[1:])
        return self.python(a[1:])

    def git(self, a):
        ok = tool.Result(0, "")
        if a[0] == "fetch":
            return ok
        if a[0] == "show" and a[1] == "origin/main:pyproject.toml":
            return tool.Result(0, 'name = "mock-edi"\nversion = "%s"\n' % self.main_version)
        if a[0] == "show" and a[1].endswith(":CHANGELOG.md"):
            return tool.Result(0, CHANGELOG)
        if a[:3] == ["rev-parse", "--verify", "--quiet"]:
            return tool.Result(0 if self.branch else 1, "")
        if a[0] == "rev-parse":
            return tool.Result(0, {"--abbrev-ref": self.on, "HEAD": self.head,
                                   "origin/main": self.origin}[a[1]])
        if a[0] == "ls-remote" and "--heads" in a:
            return tool.Result(0, "def\trefs/heads/release/%s\n" % VERSION
                               if self.remote_branch else "")
        if a[0] == "ls-remote":
            return tool.Result(0, "abc\trefs/tags/v%s\n" % VERSION if self.tag else "")
        if a[0] == "status":
            return tool.Result(0, " M x\n" if self.dirty else "")
        if a[0] == "switch":
            if "-c" in a:
                self.branch = True
            return ok
        if a[:2] == ["diff", "--cached"]:
            return tool.Result(1, "")
        if a[0] == "log":
            return tool.Result(0, self.merge_sha + "\n" if self.main_version == VERSION else "")
        if a[0] == "tag":
            self.tagged_at = a[3]
            return ok
        if a[0] == "push" and a[-1] == "v" + VERSION:
            self.tag = True
        return ok

    def gh(self, a):
        ok = tool.Result(0, "")
        if a[:2] == ["pr", "list"] and "--label" in a:
            return tool.Result(0, "\n".join(self.labelled))
        if a[:2] == ["pr", "list"]:
            return tool.Result(0, str(self.open_pr) if self.open_pr else "")
        if a[:2] == ["release", "view"]:
            return tool.Result(0 if self.gh_release else 1, "")
        if a[:2] == ["run", "list"] and tool.CI_WORKFLOW in a:
            return tool.Result(0, "\n".join(self.ci))
        if a[:2] == ["run", "list"] and tool.PUBLISH_WORKFLOW in a:
            if not self.gh_release:
                return ok
            now = self.publish.pop(0) if len(self.publish) > 1 else self.publish[0]
            if now == ["completed success"] and self.pypi_after_publish:
                self.pypi = True
            return tool.Result(0, "\n".join(now))
        if a[:2] == ["pr", "create"]:
            self.open_pr = 42
            return ok
        if a[:2] == ["pr", "checks"]:
            now = self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]
            return tool.Result(0, "\n".join(now))
        if a[:2] == ["pr", "merge"]:
            assert "--merge" in a, a
            self.main_version, self.open_pr, self.branch = VERSION, 0, False
            return ok
        if a[:2] == ["release", "create"]:
            with open(a[a.index("--notes-file") + 1], encoding="utf-8") as handle:
                self.notes = handle.read()
            self.gh_release = True
            return ok
        raise AssertionError("unexpected gh %s" % a)

    def python(self, a):
        if a[0] == "tools/check_changelog.py" and len(a) == 1:
            return tool.Result(0 if self.intro_written else 1, "placeholder")
        return tool.Result(0, "")

    page = None

    def fetch(self, url):
        if self.page is not None:
            return self.page
        return "<a>mock_edi-%s.tar.gz</a>" % VERSION if self.pypi else "<a>mock_edi-0.5.0.tar.gz</a>"

    now = 0.0

    def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now

    def ran(self, fragment):
        return [call for call in self.calls if fragment in call]


class ReleaseCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        with open(os.path.join(self.root, "pyproject.toml"), "w") as handle:
            handle.write('[project]\nname = "mock-edi"\nversion = "0.5.0"\n')
        self.said = []

    def release(self, world, resume=False):
        return tool.Release(VERSION, run=world.run, fetch=world.fetch,
                            sleep=world.sleep, clock=world.clock,
                            say=self.said.append, root=self.root).release(resume)

    def assertStops(self, world, fragment, code=1, resume=False):
        with self.assertRaises(tool.Stop) as caught:
            self.release(world, resume)
        self.assertIn(fragment, str(caught.exception))
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class FromAGreenMain(ReleaseCase):
    def test_one_command_takes_it_to_pypi(self):
        world = World()
        self.assertEqual(self.release(world), 0)
        self.assertTrue(world.tag and world.gh_release and world.pypi)
        self.assertEqual(world.tagged_at, world.merge_sha)
        self.assertIn("Why anyone should upgrade.", world.notes)
        self.assertNotIn("Older.", world.notes)
        self.assertIn("is released", self.said[-1])

    def test_the_steps_run_in_order(self):
        world = World()
        self.release(world)
        order = [world.ran(step)[0] for step in (
            "check_changelog.py --release", "unittest discover", "pr create",
            "pr merge", "tag -a", "release create")]
        self.assertEqual(order, sorted(order, key=world.calls.index))

    def test_it_bumps_the_version(self):
        self.release(World())
        with open(os.path.join(self.root, "pyproject.toml")) as handle:
            self.assertIn('version = "0.6.0"', handle.read())

    def test_the_suite_runs_once(self):
        world = World()
        self.release(world)
        self.assertEqual(len(world.ran("unittest discover")), 1)


class ItRefusesAMainNotFitToRelease(ReleaseCase):
    def test_uncommitted_changes(self):
        self.assertStops(World(dirty=True), "uncommitted changes")

    def test_another_branch(self):
        self.assertStops(World(on="feature"), "not on main")

    def test_behind_origin(self):
        self.assertStops(World(origin="n3w"), "not level with origin/main")

    def test_ci_not_green(self):
        self.assertStops(World(ci=["completed failure"]), "CI is not green")
        self.assertStops(World(ci=[]), "no run")

    def test_a_pull_request_labelled_for_this_release_is_open(self):
        self.assertStops(World(labelled=["77"]), "#77")

    def test_and_changes_nothing_when_it_refuses(self):
        world = World(dirty=True)
        self.assertStops(world, "uncommitted")
        self.assertEqual(world.ran("switch"), [])


class TheOneDeliberateStop(ReleaseCase):
    def test_it_stops_for_the_paragraph_and_resume_finishes(self):
        world = World(intro_written=False)
        stop = self.assertStops(world, "why anyone should upgrade", code=3)
        self.assertIn("release/0.6.0", str(stop))
        self.assertEqual(world.ran("pr create"), [])
        world.intro_written = True
        self.assertEqual(self.release(world, resume=True), 0)
        self.assertTrue(world.pypi)
        self.assertEqual(len(world.ran("check_changelog.py --release")), 1)


class ResumingFromWhatExists(ReleaseCase):
    def test_the_040_case_merged_and_never_tagged(self):
        world = World(main_version=VERSION)
        self.assertEqual(self.release(world, resume=True), 0)
        self.assertEqual(world.tagged_at, world.merge_sha)
        self.assertTrue(world.gh_release and world.pypi)
        self.assertEqual(world.ran("pr create") + world.ran("pr merge"), [])

    def test_an_open_release_pull_request_is_waited_for_and_merged(self):
        world = World(open_pr=42, branch=True)
        self.release(world, resume=True)
        self.assertEqual(len(world.ran("pr merge 42")), 1)
        self.assertEqual(world.ran("pr create"), [])

    def test_tagged_but_no_github_release(self):
        world = World(main_version=VERSION, tag=True)
        self.release(world, resume=True)
        self.assertEqual(world.ran("tag -a"), [])
        self.assertTrue(world.gh_release)

    def test_part_way_without_resume_is_refused(self):
        self.assertStops(World(main_version=VERSION), "--resume")

    def test_resume_with_nothing_to_resume_is_refused(self):
        self.assertStops(World(), "nothing of 0.6.0 exists", resume=True)

    def test_a_finished_release_is_left_alone(self):
        world = World(main_version=VERSION, tag=True, gh_release=True, pypi=True)
        self.assertEqual(self.release(world, resume=True), 0)
        self.assertIn("Nothing to do", self.said[-1])
        self.assertEqual(world.ran("tag -a") + world.ran("release create"), [])


class EveryWaitHasALimit(ReleaseCase):
    def test_checks_that_never_finish(self):
        stop = self.assertStops(World(checks=[["pending"]]), "#42's checks to pass")
        self.assertIn("45 minutes", str(stop))

    def test_checks_that_fail(self):
        self.assertStops(World(checks=[["pass", "fail"]]), "did not pass (fail)")

    def test_a_publish_that_fails(self):
        self.assertStops(World(publish=[["completed failure"]]),
                         "finished completed failure")

    def test_pypi_that_never_lists_it(self):
        stop = self.assertStops(World(pypi_after_publish=False),
                                "PyPI to list mock-edi 0.6.0")
        self.assertIn("--resume", str(stop))


class AskingPyPI(unittest.TestCase):
    def listed(self, version, page):
        world = World(page=page)
        return tool.Release(version, run=world.run, fetch=world.fetch).listed_on_pypi()

    def test_a_later_patch_does_not_count_as_this_one(self):
        # From the review of #162: 0.1.1 is not on PyPI because 0.1.10 is.
        page = '<a href="x">mock_edi-0.1.10.tar.gz</a><a>mock_edi-0.1.10-py3-none-any.whl</a>'
        self.assertFalse(self.listed("0.1.1", page))
        self.assertTrue(self.listed("0.1.10", page))

    def test_a_wheel_or_an_sdist_counts(self):
        self.assertTrue(self.listed("0.1.1", "<a>mock_edi-0.1.1-py3-none-any.whl</a>"))
        self.assertTrue(self.listed("0.1.1", "<a>mock-edi-0.1.1.tar.gz</a>"))
        self.assertTrue(self.listed("0.1.1", "<a>mock_edi-0.1.1.zip</a>"))

    def test_an_index_that_cannot_be_reached_is_not_yet(self):
        def down(url):
            raise OSError("unreachable")
        world = World()
        self.assertFalse(tool.Release("0.1.1", run=world.run, fetch=down).listed_on_pypi())


class ABranchPushedFromElsewhere(ReleaseCase):
    def test_it_counts_as_started(self):
        # A run on another machine pushed release/X.Y.Z and stopped before
        # opening the pull request.
        self.assertStops(World(remote_branch=True), "--resume")


class TheReleaseNotes(unittest.TestCase):
    def test_the_section_without_its_heading_or_the_next(self):
        notes = tool.section(CHANGELOG, "0.6.0")
        self.assertTrue(notes.startswith("Why anyone should upgrade."))
        self.assertIn("- A thing ([#1]).", notes)
        self.assertNotIn("0.5.0", notes)

    def test_a_version_with_no_section(self):
        self.assertEqual(tool.section(CHANGELOG, "9.9.9"), "")

    def test_a_malformed_version_is_refused(self):
        with self.assertRaises(tool.Stop):
            tool.Release("0.6")


if __name__ == "__main__":
    unittest.main(verbosity=2)
