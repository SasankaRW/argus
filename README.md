# Argus

Argus is a local-first orchestrator for LLM workflows: it runs jobs on your local models (Ollama) and
escalates to Claude only when needed. **Helios** is its dashboard.

Design documents (versions tracked in the Argus Docs Index):

- Argus Core Design: how `argusd` works (queue, leases, checkpoints, stability)
- Argus Development Plan: milestones and tasks
- Argus Plugin Guide: how to add features as plugins

## Status

The version is in `VERSION`; what changed is in `CHANGELOG.md`. Core steps done: C1 skeleton, C2 store, C3 jobs, C4 workers,
C5 live events, C6 Helios live map, C7 models and escalation.

## Run it on Windows (PC, development)

```powershell
cd G:\Projects\argus
.\scripts\dev.ps1 setup     # creates .venv, installs Argus + dev tools, copies example config
.\scripts\dev.ps1 test      # runs the test suite
.\scripts\dev.ps1 check     # validates argus.yaml and the database
.\scripts\dev.ps1 run       # starts argusd on http://127.0.0.1:8600
.\scripts\dev.ps1 worker    # (second window) starts a worker with the demo plugin
.\scripts\dev.ps1 events    # (third window) watches events live
.\scripts\dev.ps1 demo      # (fourth window) submits a demo job
```

Then open http://127.0.0.1:8600 for Helios, the live map (`/lite` is the small status page).

## Layout

```
core/argus/        the argusd package
  config.py        argus.yaml + .env loading and validation
  db/              SQLite store: single writer, migrations
  jobs/            job state machine, leases, retries, watchdog
  api/             HTTP API (FastAPI)
  worker/          argus-worker: client, workflow runner, demo plugin
  models/          Ollama and Claude providers, tier router (checks, escalation), doctor
  modelboard.py    circuit breakers and the daily Claude budget, shared by all workers
  registry.py      workers, components and edges (the Helios map)
  events.py        event log, edges, live stream hub, retention
  tail.py          argus-events: live events in the terminal
  helios_dist/     built Helios, served at /helios
tests/             unit, state machine and crash tests
runner/            desktop runner for the PC (later)
plugins/           one folder per plugin (M2)
helios/            Helios source (React); built copy in core/argus/helios_dist
cli/               argus command: docs sync, release, deploy
deploy/            laptop deployment (MD)
docs/              exported docs and diagrams
scripts/           dev helpers
```

## Versioning and workflow

- Argus uses `MAJOR.MINOR.PATCH` in `VERSION`; every release has a `CHANGELOG.md` entry, a Git tag `vX.Y.Z`
  and a GitHub release. Database schema changes are numbered migrations; Argus refuses a newer database.
- Work goes on a branch, into a pull request, and is squash-merged when CI is green; `main` is protected.
  Releases are one command: `.\scripts\dev.ps1 release minor`. Details: [CONTRIBUTING.md](CONTRIBUTING.md).
- Design docs are versioned in [docs/](docs/README.md).
