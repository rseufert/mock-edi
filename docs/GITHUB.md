# GitHub settings

The settings `mock-sap`, `mock-edi` and `mock-bank` are kept at, and the reason
for each. This file is identical in all three repositories, so a diff between
any two copies is drift.

It is about the repository, not the code: what GitHub itself is configured to
allow. The reasons all come back to the same two facts. These are packages
strangers install from PyPI, and they are maintained by **one account** with no
other collaborator. So the risk is not an untrusted colleague — there isn't one.
It is a compromised account, and it is the maintainer's own mistake at four in
the afternoon. Every rule below is one of those two.

## Releasing, which is the part that cannot be taken back

A published version is the only thing here that no later commit can fix. PyPI
refuses a re-upload of a version it already has, so a wrong artifact is wrong
for good — the only remedy is yanking it and burning the number.

| Setting | Required | Why |
| --- | --- | --- |
| `pypi` environment: deployment branches and tags | `v*` tags only | `publish.yml` runs `on: release: published` **and** `workflow_dispatch`. The only thing keeping a manual dispatch away from PyPI is the job's own `if: github.event_name == 'release'`, one line in a file any pull request can change; this makes it a setting as well. |
| `pypi` environment: required reviewers | the maintainer | Trusted Publishing has no API token, which is the point — but it also means there is no token to revoke once something is wrong. This is the only place a second deliberate act can be required, and reviewing your own deployment is not theatre: it turns a mis-click into a prompt. |
| Tag ruleset on `v*` | no deletion, no non-fast-forward | A PyPI version and its release page are keyed to a tag. A tag that moves leaves the published artifact pointing at history that no longer produced it, and because the version cannot be re-uploaded, that cannot be corrected afterwards. |
| `pypa/gh-action-pypi-publish` | pinned to a commit SHA | It is the only third-party action that runs with `id-token: write`. `@release/v1` is a branch: whatever is on it at the time is what gets the OIDC token. |
| Actions allowed | `selected` — `actions/*` and the publish action | `all` means any action at any version may run in a repository that can publish to PyPI. |
| `GITHUB_TOKEN` default permissions | read | Already the case everywhere. A workflow that needs to write asks for it in its own `permissions:` block, where it is visible in review. |

## `main`

| Setting | Required | Why |
| --- | --- | --- |
| Branch ruleset, **default branch only** | no deletion, no non-fast-forward | The guardrail against a bad `git push --force`, not a statement about trust. A ruleset that exists but is set to `disabled` provides none of this; check the enforcement, not the presence. Scope it to `main` and **not** to all refs: `mock-bank`'s `CONTRIBUTING.md` tells you to rebase a branch onto `main` as often as you like while it is still only yours, and rebasing a pushed branch is a force-push. A non-fast-forward rule across every ref forbids the workflow the project documents. What must never be rewritten is `main`. |
| Required status check | one aggregating job | Make "merge it when green" something the repository enforces rather than something a person remembers. Require a single job that `needs:` the others — the matrix check names (`tests (py3.12 on macos-latest)`) are generated and change whenever the matrix does, so requiring them by name breaks on the next Python release. |
| Direct pushes to `main` | allowed, but not the habit | With one maintainer, required reviews would mean either blocking the only person who can approve or granting a bypass that makes the rule decorative. The convention is a branch and a pull request; the ruleset above is what stops the irreversible kinds of mistake. |

## Branches and merging

| Setting | Required | Why |
| --- | --- | --- |
| Merge method | exactly one, per the table below | Both repositories that wrote down a rule gave a reason for it. The setting should make the rule true rather than leave it to memory. |
| Delete branch on merge | on | A merged branch is finished. Keeping it means `git branch -r` stops being a list of work in progress. |
| Allow auto-merge | on | "Merge it when green" becomes a setting instead of a vigil: set it once and CI merges, rather than someone watching checks. |
| Allow update branch | on | Bring a stale pull request up to date without a local merge and a push. |
| Secret scanning and push protection | on | Free on public repositories. Push protection is the half that matters: it refuses the push rather than reporting the leak afterwards. |
| Dependabot security updates, plus `.github/dependabot.yml` for `github-actions` | on | These packages have zero dependencies, so the entire surface is the Actions they run. That is also what makes it worth having: pinning actions to SHAs only stays safe if something keeps the pins current, and without that, pinning is what people quietly stop doing. |
| Wiki, Projects | off unless in use | An enabled feature nobody uses is a second place for documentation to exist and go stale. |

## What is deliberately not uniform

The merge method. Both repositories that documented one cite the *same* fact —
a squash writes a new commit, so a squashed branch never becomes an ancestor of
`main` and `git branch --merged` will not list it — and draw opposite
conclusions from it. That is a judgement about history, and it is settled per
repository, not here.

| Repository | Merge method | Where the reason is written |
| --- | --- | --- |
| `mock-sap` | squash | **Nowhere yet.** It is the practice — every merge to `main`, including the one outside contribution taken so far — but no file states it or gives the reason. Until it is written into `CONTRIBUTING.md`, it is a habit, and this table is the only place it is recorded. |
| `mock-edi` | merge commit | `CONTRIBUTING.md` — squash and rebase are switched off so the rule cannot be bypassed by habit |
| `mock-bank` | squash | `CONTRIBUTING.md` |

The rule this file does impose is that whichever one a repository chose, the
other two are **switched off**. A documented convention that the settings still
permit is a convention that holds until someone is in a hurry.

## Issues

A shared core of labels, so the same words mean the same thing when work moves
between repositories: `bug`, `enhancement`, `documentation`, `question`,
`duplicate`, `invalid`, `wontfix`, `help wanted`, `good first issue`,
`accessibility`, `blocked`, `no changelog`, and the triage set `P1`/`P2`/`P3`
with `senior`/`junior`. A repository may add its own beyond that; it should not
be missing one of these.

`no changelog` and the priority labels are the maintainer's to apply. Note that
`tools/check_changelog.py` only asks for an entry when a pull request touches
the package, so a documentation-only change needs neither an entry nor the
label.

## Not covered

`mock-films` is private on a plan where rulesets and environment protection are
unavailable, and it publishes nothing. It is not held to this file. If it ever
goes public, it is.

## Checking a repository against this

Settings drift silently and nothing in CI can see them, so this is the only
check there is:

```bash
for r in mock-sap mock-edi mock-bank; do
  echo "== $r"
  gh api repos/rseufert/$r --jq '"  squash=\(.allow_squash_merge) commit=\(.allow_merge_commit) rebase=\(.allow_rebase_merge)",
    "  automerge=\(.allow_auto_merge) delbranch=\(.delete_branch_on_merge) updatebranch=\(.allow_update_branch)"'
  gh api repos/rseufert/$r/rulesets --jq '.[] | "  ruleset: \(.name) target=\(.target) \(.enforcement)"'
  gh api repos/rseufert/$r/environments --jq '.environments[] | "  env: \(.name) rules=\(.protection_rules | length)"'
  gh api repos/rseufert/$r/actions/permissions --jq '"  actions: \(.allowed_actions // "all")"'
done
```

A `pypi` environment with `rules=0`, or a ruleset reading `disabled`, is the
same as not having it.
