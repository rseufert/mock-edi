"""The workflow files, held to the two rules a reader would not notice broken.

`main` requires one check, the `all-green` job in `ci.yml`. A job that is not
in its `needs:` can fail and the pull request still merges, and nothing goes
red to say so. And an action from outside GitHub must be pinned to a commit:
a branch or a tag there can be moved by whoever holds that repository.

The files are read as text. There is no YAML parser in the standard library,
and these two rules need none.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(os.path.dirname(HERE), ".github", "workflows")

REQUIRED = "all-green"


def read(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as handle:
        return handle.read()


def jobs(text):
    """Each job's id and its own lines, from the `jobs:` block of a workflow."""
    body = text.split("\njobs:\n", 1)[1]
    found, current = {}, None
    for line in body.splitlines():
        started = re.match(r"  ([\w-]+):\s*$", line)
        if started:
            current = found.setdefault(started.group(1), [])
        elif current is not None:
            current.append(line)
    return found


def needs(lines):
    for line in lines:
        listed = re.match(r"\s+needs:\s*\[(.*)\]\s*$", line)
        if listed:
            return {name.strip() for name in listed.group(1).split(",")}
    return set()


class TheJobThatMainRequires(unittest.TestCase):

    def setUp(self):
        self.jobs = jobs(read("ci.yml"))

    def test_it_needs_every_other_job(self):
        others = set(self.jobs) - {REQUIRED}
        self.assertTrue(others)
        self.assertEqual(needs(self.jobs[REQUIRED]), others)

    def test_it_runs_when_a_job_it_needs_did_not_pass(self):
        """Skipped, it would count as passed, and the rule would hold nothing."""
        self.assertIn("    if: always()", self.jobs[REQUIRED])

    def test_it_keeps_the_name_the_ruleset_asks_for(self):
        self.assertIn("    name: all checks passed", self.jobs[REQUIRED])

    def test_only_success_passes(self):
        text = "\n".join(self.jobs[REQUIRED])
        self.assertIn("toJSON(needs)", text)
        self.assertIn('all(.[]; .result == "success")', text)


class ActionsFromOutsideGitHub(unittest.TestCase):
    """`actions/` stands for GitHub's own here, which is all the workflows use.

    GitHub has others, `github/codeql-action` among them, and the repository's
    setting allows them. One of those would fail the last test below with a
    message about the publish action: widen what counts as GitHub's then.
    """

    def uses(self):
        for name in sorted(os.listdir(WORKFLOWS)):
            for line in read(name).splitlines():
                used = re.match(r"\s*-?\s*uses:\s*(\S+)(.*)$", line)
                if used:
                    yield name, used.group(1), used.group(2)

    def test_each_is_pinned_to_a_commit_and_says_which_version(self):
        outside = [use for use in self.uses()
                   if not use[1].startswith("actions/")]
        self.assertTrue(outside)
        for name, action, rest in outside:
            with self.subTest(workflow=name, action=action):
                self.assertRegex(action, r"@[0-9a-f]{40}$")
                self.assertRegex(rest, r"^\s+# v\d+\.\d+\.\d+$")

    def test_the_publish_action_is_the_only_one(self):
        """The repository allows GitHub's own actions and this one, no other."""
        outside = {action.split("@")[0] for _, action, _ in self.uses()
                   if not action.startswith("actions/")}
        self.assertEqual(outside, {"pypa/gh-action-pypi-publish"})


if __name__ == "__main__":
    unittest.main()
