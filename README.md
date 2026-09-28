# Argus

Argus is a local-first orchestrator for LLM workflows: it runs jobs on your local models (Ollama) and
escalates to Claude only when needed. **Helios** is its dashboard.

Design documents (versions tracked in the Argus Docs Index):

- Argus Core Design: how `argusd` works (queue, leases, checkpoints, stability)
- Argus Development Plan: milestones and tasks
- Argus Plugin Guide: how to add features as plugins

## Status

Version `0.1.0` (see `VERSION` and `CHANGELOG.md`). Core steps done: C1 skeleton, C2 store, C3 jobs.

## Run it on Windows (PC, development)

```powershell
cd G:\Projects\argus
.\scripts\dev.ps1 setup     # creates .venv, installs Argus + dev tools, copies example config
.\scripts\dev.ps1 test      # runs the test suite
.\scripts\dev.ps1 check     # validates argus.yaml and the database
.\scripts\dev.ps1 run       # starts argusd on http://127.0.0.1:8600
```

Then open http://127.0.0.1:8600/health.

## Layout

```
core/argus/        the argusd package
  config.py        argus.yaml + .env loading and validation
  db/              SQLite store: single writer, migrations
  jobs/            job state machine, leases, retries, watchdog
  api/             HTTP API (FastAPI)
tests/             unit, state machine and crash tests
runner/            desktop runner (C4)
plugins/           one folder per plugin (M2)
helios/            dashboard (C6)
cli/               argus command: docs sync, release, deploy
deploy/            laptop deployment (MD)
docs/              exported docs and diagrams
scripts/           dev helpers
```

## Versioning

- Argus uses `MAJOR.MINOR.PATCH`. The number lives in `VERSION`; every release adds a `CHANGELOG.md` entry
  and a Git tag `vX.Y.Z`.
- Database schema changes are numbered migrations in `core/argus/db/migrations/`. Argus refuses to open a
  database newer than itself.
