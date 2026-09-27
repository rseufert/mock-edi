"""The changelog checks, and the release that assembles them.

`tools/check_changelog.py` had no tests: it guards a file rather than the
package, and it was small enough to read. Moving every waiting entry into
`changelog.d/` makes it the thing that decides whether an entry was lost, and
that is worth holding down - the check exists because an entry once went
missing in a merge and came back by luck.

The strongest test here is the last one: 0.5.0, split back into fragments and
reassembled, entry for entry.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import check_changelog as tool

PYPROJECT = 'version = "1.2.3"\n'

CHANGELOG = """# Changelog

## [Unreleased]

Waiting entries live in `changelog.d/`.

## [1.2.3] - 2026-01-02

Why you should upgrade.

### Fixed

- Something was wrong ([#1]).

[Unreleased]: https://example.test/compare/v1.2.3...HEAD
[1.2.3]: https://example.test/compare/v1.2.2...v1.2.3
[#1]: https://example.test/issues/1
"""


class NamingAFragment(unittest.TestCase):
    def test_a_number_and_a_kind_is_enough(self):
        for name in ("131.fixed.md", "9.added.md", "12.changed.md"):
            self.assertTrue(tool.FRAGMENT.match(name), name)

    def test_one_issue_can_have_two_entries_of_a_kind(self):
        # #125 was three people's work and left two `added` entries in 0.5.0.
        # Without this they would have collided on one file name.
        first = tool.FRAGMENT.match("125.added.md")
        second = tool.FRAGMENT.match("125-writers.added.md")
        self.assertTrue(second)
        self.assertEqual(first.group("number"), second.group("number"))
        self.assertEqual(second.group("slug"), "writers")

    def test_a_kind_that_is_not_a_heading_is_refused(self):
        for name in ("131.broken.md", "131.removed.md", "notes.md", "131.md"):
            self.assertIsNone(tool.FRAGMENT.match(name), name)

    def test_check_fragments_says_which_file_and_why(self):
        problems = tool.check_fragments({"131.broken.md": "- x\n"})
        self.assertEqual(len(problems), 1)
        self.assertIn("131.broken.md", problems[0])
        self.assertIn("added, changed, fixed", problems[0])

    def test_a_fragment_with_no_bullet_is_refused(self):
        problems = tool.check_fragments({"131.fixed.md": "just some prose\n"})
        self.assertEqual(len(problems), 1)
        self.assertIn("holds no entry", problems[0])

    def test_a_well_formed_one_passes(self):
        self.assertEqual(tool.check_fragments(
            {"131.fixed.md": "- **A thing** ([#131]).\n  More about it.\n"}), [])


class TheUnreleasedSection(unittest.TestCase):
    def test_it_may_hold_prose_but_not_entries(self):
        self.assertEqual(tool.check_structure(CHANGELOG, PYPROJECT), [])

    def test_an_entry_left_there_is_reported(self):
        text = CHANGELOG.replace("Waiting entries live in `changelog.d/`.",
                                 "- **Something** ([#1]).")
        problems = tool.check_structure(text, PYPROJECT)
        self.assertEqual(len(problems), 1)
        self.assertIn("changelog.d/", problems[0])


class TheIntroPlaceholder(unittest.TestCase):
    """A gap only a reader would notice ships one day; a failing check does not."""

    def test_a_section_still_holding_it_fails(self):
        text = CHANGELOG.replace("Why you should upgrade.",
                                 tool.INTRO_PLACEHOLDER)
        problems = tool.check_structure(text, PYPROJECT)
        self.assertEqual(len(problems), 1)
        self.assertIn(tool.INTRO_TODO, problems[0])

    def test_it_is_visible_rather_than_an_html_comment(self):
        # If it ever escapes the check it should escape loudly.
        self.assertNotIn("<!--", tool.INTRO_PLACEHOLDER)
        self.assertIn(tool.INTRO_TODO, tool.INTRO_PLACEHOLDER)


class NothingWaitingDisappears(unittest.TestCase):
    """The rule the whole tool exists for, now reading fragments."""

    def test_a_fragment_that_vanished_is_reported(self):
        waited = {"1.fixed.md": "- **A thing** ([#1]).\n"}
        problems = tool.check_against_base(CHANGELOG, CHANGELOG, "HEAD",
                                           waiting={}, waited=waited)
        self.assertTrue(any("A thing" in p for p in problems), problems)

    def test_one_that_moved_into_a_release_is_not(self):
        waited = {"1.fixed.md": "- Something was wrong ([#1]).\n"}
        problems = tool.check_against_base(CHANGELOG, CHANGELOG, "HEAD",
                                           waiting={}, waited=waited)
        self.assertEqual(problems, [])

    def test_one_that_is_still_waiting_is_not(self):
        waiting = waited = {"1.fixed.md": "- **A thing** ([#1]).\n"}
        self.assertEqual(tool.check_against_base(CHANGELOG, CHANGELOG, "HEAD",
                                                 waiting=waiting,
                                                 waited=waited), [])

    def test_an_entry_migrated_out_of_unreleased_is_not(self):
        # The one pull request that moves what was under the heading into
        # fragments: the entry has not gone, it has moved.
        before = CHANGELOG.replace("Waiting entries live in `changelog.d/`.",
                                   "- **A thing** ([#1]).")
        problems = tool.check_against_base(
            CHANGELOG, before, "HEAD",
            waiting={"1.added.md": "- **A thing** ([#1]).\n"}, waited={})
        self.assertEqual(problems, [])

    def test_a_released_section_that_changed_is_still_reported(self):
        # Unchanged behaviour, checked because the rule moved house.
        text = CHANGELOG.replace("- Something was wrong ([#1]).",
                                 "- Something else was wrong ([#1]).")
        problems = tool.check_against_base(text, CHANGELOG, "HEAD")
        self.assertTrue(any("already released" in p for p in problems), problems)


class AssemblingARelease(unittest.TestCase):
    def fragments(self):
        return {"1.added.md": "- **First** ([#1]).\n  With a second line.\n",
                "2.fixed.md": "- **Second** ([#2]).\n",
                "10.added.md": "- **Tenth** ([#10]).\n"}

    def assemble(self):
        return tool.assemble(CHANGELOG, "1.3.0", self.fragments(),
                             today="2026-02-03")

    def test_the_headings_are_added_changed_fixed_in_that_order(self):
        section = dict((v, b) for v, _, b in
                       tool.sections(self.assemble()))["1.3.0"]
        self.assertEqual(re.findall(r"^### (\w+)", section, flags=re.M),
                         ["Added", "Fixed"])

    def test_entries_keep_their_own_words_and_indent(self):
        section = dict((v, b) for v, _, b in
                       tool.sections(self.assemble()))["1.3.0"]
        self.assertIn("- **First** ([#1]).\n  With a second line.", section)

    def test_they_are_in_issue_number_order_not_string_order(self):
        section = dict((v, b) for v, _, b in
                       tool.sections(self.assemble()))["1.3.0"]
        self.assertLess(section.index("**First**"), section.index("**Tenth**"))

    def test_the_new_section_goes_above_the_previous_release(self):
        versions = [v for v, _, _ in tool.sections(self.assemble())]
        self.assertEqual(versions, ["Unreleased", "1.3.0", "1.2.3"])

    def test_the_links_are_written_and_unreleased_is_re_pointed(self):
        text = self.assemble()
        self.assertIn("[Unreleased]: https://example.test/compare/v1.3.0...HEAD",
                      text)
        self.assertIn("[1.3.0]: https://example.test/compare/v1.2.3...v1.3.0",
                      text)

    def test_the_intro_is_a_placeholder_the_check_refuses(self):
        text = self.assemble()
        self.assertIn(tool.INTRO_TODO, text)
        self.assertTrue(any(tool.INTRO_TODO in p for p in
                            tool.check_structure(text, PYPROJECT)))

    def test_a_dated_heading_is_written(self):
        self.assertIn("## [1.3.0] - 2026-02-03", self.assemble())


class TheRealReleaseRoundTrips(unittest.TestCase):
    """0.5.0, split back into fragments and reassembled.

    The one test that compares against something a person actually wrote. Entry
    for entry rather than character for character, because the *order* entries
    appear in under a heading is editorial - 0.5.0 put #127 before #125 because
    it read better - and no tool can reproduce a judgement.
    """

    def setUp(self):
        self.real = tool._read("CHANGELOG.md")
        released = [v for v, _, _ in tool.sections(self.real) if v != "Unreleased"]
        if not released:
            self.skipTest("no released section to take apart")
        self.version = released[0]
        self.body = dict((v, b) for v, _, b in
                         tool.sections(self.real))[self.version]

    def split(self):
        _intro, _, rest = self.body.partition("\n### ")
        out, seen = {}, {}
        for group in re.split(r"^### ", "### " + rest, flags=re.M):
            if not group.strip():
                continue
            head, _, text = group.partition("\n")
            kind = head.strip().lower()
            if kind not in tool.KINDS:
                continue
            for entry in re.split(r"\n(?=- )", text.strip("\n")):
                if not entry.strip():
                    continue
                found = re.search(r"\(\[#(\d+)\]\)", entry)
                number = found.group(1) if found else "0"
                seen[(number, kind)] = seen.get((number, kind), 0) + 1
                slug = "" if seen[(number, kind)] == 1 else "-%d" % seen[(number, kind)]
                out["%s%s.%s.md" % (number, slug, kind)] = entry.rstrip("\n") + "\n"
        return out

    def rebuilt(self):
        waiting = self.split()
        self.assertTrue(waiting, "the released section yielded no entries")
        self.assertEqual(tool.check_fragments(waiting), [])
        before = self.real.replace(
            "## [%s] - " % self.version, "## [x-removed] - ", 1)
        assembled = tool.assemble(before, "9.9.9", waiting, today="2026-01-01")
        return dict((v, b) for v, _, b in tool.sections(assembled))["9.9.9"]

    def test_every_entry_survives_word_for_word(self):
        self.assertEqual(sorted(tool.bullets(self.rebuilt())),
                         sorted(tool.bullets(self.body)))

    def test_the_headings_are_the_ones_it_had(self):
        self.assertEqual(re.findall(r"^### (\w+)", self.rebuilt(), flags=re.M),
                         re.findall(r"^### (\w+)", self.body, flags=re.M))


if __name__ == "__main__":
    unittest.main()
