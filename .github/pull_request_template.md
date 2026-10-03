<!-- Title: type(scope): what changed  (e.g. "fix(phone): only claim phone jobs"). See CONTRIBUTING.md. -->

## What

<!-- The change in plain words: what a user or developer will notice. -->

## Why

<!-- The problem it solves, or why now. -->

## How tested

<!-- Tests added/changed, and what was checked by hand (or "not tried on the PC yet"). -->

## Risk

<!-- What could break, and how to undo it (usually: revert this commit). -->

## Checklist

- [ ] Tests pass (`.\scripts\dev.ps1 test`) and lint is clean (`.\scripts\dev.ps1 lint`)
- [ ] `CHANGELOG.md` has a line under **Unreleased**
- [ ] Helios rebuilt if `helios/src` changed (`.\scripts\dev.ps1 helios`)
- [ ] Docs updated if behaviour or design changed (and their version bumped)
