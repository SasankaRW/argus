# Working on Argus

Every change goes the same way: **branch → one clear commit → pull request → green CI → squash-merge**.
`main` only moves through a merged pull request or a release. Claude never pushes, opens PRs, merges or
releases; it hands over a change package and you run the steps below.

## The flow

```powershell
# A change package from Claude (argus-<topic>.tgz):
.\scripts\dev.ps1 apply $HOME\Downloads\argus-<topic>.tgz   # new branch, files, changelog, Helios, tests, one commit

# Or your own change:
.\scripts\dev.ps1 branch feat/<topic>                      # fresh main, new branch
#   ...edit, then: git add -A; git commit   (the hook checks the message)

.\scripts\dev.ps1 pr                                        # push, open the PR (title + description from the commit)
.\scripts\dev.ps1 merge                                     # wait for CI, squash-merge, back on an updated main
.\scripts\dev.ps1 ship a.tgz b.tgz                          # all three for each package, in order; stops at a problem
.\scripts\dev.ps1 release minor                             # at a milestone only
```

`apply` stops without committing if the tree is dirty, the branch name was used before, the metadata is
wrong or the tests fail. Fix the cause (or ask Claude for a new package) and run it again. It runs lint and only
the tests for the changed files (a change to shared test helpers or the database runs them all); CI runs the full
suite. `$env:ARGUS_ALL_TESTS = 1` makes `apply` run everything too.

After a merge the laptop updates itself within about 5 minutes (set up once with `deploy/linux/auto-update.sh`,
see deploy/README.md); `ship` pulls main on the PC, whose supervisor restarts the worker.

## Branch names

`type/short-topic` in lower case: `feat/ari-direct-paths`, `fix/phone-claims-other-jobs`. Types are listed
below. **A name is used once.** GitHub deletes merged branches, and a reused name can attach to an old,
merged pull request (that happened with PR #35). `branch` and `apply` refuse a name any PR used before.

## Commit messages

```
type(scope): what changed, as a command

## What
The change in plain words: what a user or developer will notice.

## Why
The problem it solves or the reason for doing it now.

## How tested
Tests added or changed, and what you checked by hand (or "not tried on the PC yet").

## Risk
What could break, and how to undo it (usually: revert this commit).
```

- **Title:** at most 72 characters, no full stop, starts in lower case with a verb ("add", "fix", "stop"),
  and never "added" or "fixed". Scope is the area: `ari`, `phone`, `helios`, `outbox`, `screen`,
  `plugins`, `ci`, and so on. `!` after the scope marks a breaking change.
- **Types:** `feat` (new behaviour), `fix` (a bug), `perf` (faster, same behaviour), `refactor`
  (same behaviour, cleaner code), `docs`, `test`, `chore` (tooling, deps), `ci`, `build`.
- **One topic per branch.** If the title needs "and", it is probably two changes.
- **No Claude attribution lines** (`Co-Authored-By: Claude…`, `Claude-Session:`).

`.githooks/commit-msg` checks the title and blank line, and rejects attribution lines. CI checks the PR
title, because on squash-merge the title becomes the commit on `main`.

## Pull requests

- `dev.ps1 pr` uses the first commit's title as the PR title and its body as the description, so the
  What / Why / How tested / Risk sections carry over. Extra fixes on the branch are fine (`fix: …` commits).
  They disappear into the one squashed commit.
- The description is what a reader needs in six months: keep it accurate if the change grows
  (`gh pr edit`).
- `dev.ps1 merge` merges only what CI tested. The commit on `main` is `<PR title> (#N)` with the PR
  description as its body, and the branch is deleted.

## When CI fails

CI runs ruff and the tests on Windows and Linux (Python 3.11 to 3.13), the Helios build check and the PR
title check. `ci-ok` is the one required check.

| What failed | Do this |
|---|---|
| A test this change touches, or lint | Fix it, commit (`fix: …`), `dev.ps1 pr`, then `dev.ps1 merge` |
| `helios`: "helios_dist is out of date" | `dev.ps1 helios`, commit the rebuilt `helios_dist` (old hashed files deleted too) |
| `title` | `gh pr edit --title "type(scope): …"`, then re-run the checks |
| A test unrelated to the change, on one runner only | `dev.ps1 rerun` **once** (flaky test) |
| The same test fails again | Treat it as real: fix the test or the code in a `fix(test): …` change |

## Change packages (how Claude delivers)

A package is a `.tgz` of the changed files plus `.change/change.json`:

```json
{"branch": "perf/ari-direct-paths",
 "title": "perf(ari): answer plain status questions without the model",
 "body": "## What\n...\n\n## Why\n...\n\n## How tested\n...\n\n## Risk\n...",
 "changelog": "- **Faster:** ...",
 "deleted": ["core/argus/relay.py"]}
```

Before handing it over, Claude runs the full tests and ruff, rebuilds Helios when its source changed, and
picks a branch name that hasn't been used. Then `dev.ps1 apply` does the rest on the PC, from an updated
`main`. Apply packages in order and merge each one before applying the next, so none conflicts.

## Versions

- Argus uses `MAJOR.MINOR.PATCH` in `VERSION`: patch = fixes, minor = new features, major = breaking
  changes.
- Every pull request adds a line under `## Unreleased` in `CHANGELOG.md` (`apply` does it from the
  package). The release tool turns that section into the new version's entry, tags `vX.Y.Z` and publishes
  a GitHub release with the same notes.
- The database schema version is the migration number (`core/argus/db/migrations/NNNN_name.sql`).
- Documents in `docs/` carry their own `MAJOR.MINOR` versions (see `docs/README.md`).

## Guard rails

- `.githooks/pre-push` refuses direct pushes, force-pushes and deletion of `main`.
- `.githooks/commit-msg` enforces the commit message format.
- `dev.ps1 branch`, `apply` and `pr` set `core.hooksPath` to `.githooks`, so the hooks are on.
