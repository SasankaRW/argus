# Working on Argus

## Branches

- `main` is always releasable. It changes only through a merged pull request or a release.
- Work happens on a short branch: `feat/...`, `fix/...`, `docs/...` or `chore/...`.
- Pull requests are squash-merged, so each one is a single commit on `main`, and the branch is deleted.
- CI (tests on Windows and Linux, Python 3.11 to 3.13, and the Helios build) must pass before merging.

## The loop (PowerShell, from the repo root)

```powershell
.\scripts\dev.ps1 branch feat/models   # start: fresh main, new branch
# ...change code, commit...
.\scripts\dev.ps1 pr                   # push the branch and open a pull request
.\scripts\dev.ps1 merge                # after CI is green: squash-merge, back to main
.\scripts\dev.ps1 release minor        # at a milestone only: tests, version bump, changelog, tag, GitHub release
```

`.\scripts\dev.ps1 sync` brings `main` up to date. A local Git hook (`.githooks/pre-push`) refuses direct
pushes, force-pushes and deletion of `main`.

## Versions

- Argus uses `MAJOR.MINOR.PATCH` in `VERSION`: patch = fixes, minor = new features (each core step C1-C12
  is a minor), major = breaking changes (1.0.0 when the core API is frozen after C12).
- Every pull request adds a line under `## Unreleased` in `CHANGELOG.md`. The release tool turns that
  section into the new version's entry, tags `vX.Y.Z` and publishes a GitHub release with the same notes.
- The database schema version is the migration number (`core/argus/db/migrations/NNNN_name.sql`).
- Documents in `docs/` carry their own `MAJOR.MINOR` versions (see `docs/README.md`).
