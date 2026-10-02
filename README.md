# Argus

Argus is a local-first orchestrator for LLM workflows: it runs jobs on your local models (Ollama) and
escalates to Claude only when needed. **Helios** is its dashboard.

Design documents (versions tracked in the Argus Docs Index):

- Argus Core Design: how `argusd` works (queue, leases, checkpoints, stability)
- Argus Development Plan: milestones and tasks
- Argus Plugin Guide: how to add features as plugins

## Status

The version is in `VERSION`; what changed is in `CHANGELOG.md`. Core steps done: C1 skeleton, C2 store, C3 jobs, C4 workers,
C5 live events, C6 Helios live map, C7 models and escalation, C8 approvals, outbox and ntfy, C9 scheduler,
triggers and dispatcher.

## Run it on Windows (PC, development)

First time: follow **[docs/setup.md](docs/setup.md)** (tools, models, Claude, `.env`, phone, plugins).

```powershell
cd G:\Projects\argus
.\scripts\dev.ps1 setup     # once: creates .venv, installs Argus + dev tools, copies example config
.\scripts\dev.ps1 up        # starts Ollama, argusd and a worker in the background, then opens Helios (or double-click scripts\up.cmd)
.\scripts\dev.ps1 down      # stops what "up" started ("down all" also stops Ollama)
.\scripts\dev.ps1 logs      # every log in one terminal (also Helios > Logs); "status" shows what runs
```

Trying things out: `demo` (a small job), `classify` (a model job with an escalation), `approval` (a pretend bill
that waits for you), `notify` (a test notification), `models` (checks Ollama and Claude). `test` runs the tests;
`run` and `worker` start the parts by hand. Helios is at http://127.0.0.1:8600 (`/lite` is the small status page).

Phone: install the Argus app (`android/README.md`) and sign it in; it collects your notifications itself, with the
app closed, and Approve / Reject work over Tailscale. Set `approvals.phone` in `argus.yaml` to hear "back online".

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
  approvals.py     approvals: signed one-time phone tokens, reminders, expiry
  outbox.py        messages that leave Argus (ntfy), sent exactly once with retries
  presence.py      phone on Tailscale: push waiting approvals when it comes back
  relay.py         optional: approval buttons through an ntfy reply topic
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
  Releases happen at milestones, not after every merge: `.\scripts\dev.ps1 release minor`. Details:
  [CONTRIBUTING.md](CONTRIBUTING.md).
- Design docs are versioned in [docs/](docs/README.md).
