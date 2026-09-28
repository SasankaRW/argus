# Changelog

All notable changes to Argus. Versions follow `MAJOR.MINOR.PATCH`. New entries go under **Unreleased**;
`dev.ps1 release` turns that section into the next version.

## Unreleased

## 0.5.1 (2026-09-28)

- **Helios updates show up on reload:** `index.html` is now served with `Cache-Control: no-cache` (hashed assets
  stay cached), so a browser no longer keeps showing an old Helios after an upgrade.
- **Model check ignores case:** `dev.ps1 models` finds `Qwen2.5:latest` when the config says `qwen2.5:latest`,
  as Ollama does.

## 0.5.0 (2026-09-28)

Core step C7: models, tiers and escalation.

- **`ctx.llm()` for plugins:** ask the cheapest model tier that can do the job. Every answer is checked in
  code (JSON parsed and validated against a Pydantic schema, then the plugin's own check). A rejected answer
  is retried once with the reason, then escalated T1 -> T2 -> T3, and the next tier is told what the smaller
  model said and why it was rejected. `ctx.claude()` asks Claude directly. The step records which tier
  answered, and a checkpointed step never asks again after a crash.
- **Ollama provider:** `/api/chat` with structured JSON output, temperature 0, `keep_alive` so the model stays
  loaded, and a hard timeout (a hung model never hangs a worker).
- **Claude provider:** the `claude` CLI in print mode with every tool removed (`--disallowedTools "*"`,
  one turn, no saved session): text in, text out. Daily cap (default 30 calls) enforced across all workers.
- **Circuit breakers** per tier, kept in argusd: 3 failed calls in a row pause a model for 60 s, then one
  trial call; jobs move on to the next tier instead of piling up. Wrong answers don't count as failures.
- **Helios:** the model tiers are boxes stacked in one column (T1, T2, T3) showing ready / paused and call
  counts; escalations draw an amber line from tier to tier; a paused model turns red; replies travel back
  along the request's line. Click a model for its state, calls, last error and Claude budget. New Models
  filter in the events list.
- **Tools:** `dev.ps1 models` checks Ollama, pulled models and Claude (with a real test call);
  `dev.ps1 classify` runs a demo model job with T1 rejected on purpose, to watch an escalation.
- **API:** `GET /models`, `POST /models/{tier}/permit`, `POST /models/{tier}/report`, `POST /jobs/{id}/events`
  (a worker's trace events); worker registration now hands out the model configuration. Migration 0003.
- **Config:** new `models` and `claude` sections, and `ollama.timeout_seconds` / `ollama.keep_alive`.
- **Tests:** 157, with a fake Ollama (garbage, hang, errors, missing model) and a fake `claude`.

- **Version control:** GitHub connection (`dev.ps1 github`), branch and pull request workflow
  (`branch`, `pr`, `merge`, `sync`), one-command releases (`dev.ps1 release patch|minor|major`), a
  pre-push hook that protects `main`, consistent line endings, and the design docs exported into `docs/`.

## 0.4.0 (2026-09-28)

Core step C6: Helios v0, the live map.

- **Helios** at http://127.0.0.1:8600 (`/` now opens it; the small page moved to `/lite`). Built with
  React, React Flow and ELK in the Helios mockup style (dark, Geist, amber accent). Argus serves the built
  copy, so no Node.js is needed to use it.
- **Live map:** every component is a box, every pair that has talked is a line. A dot travels along a line
  for each message (red for failures), lines get thicker with traffic, boxes light up while busy, new boxes
  fade in with a "new" badge. Workers show online/offline; plugins show running and queued jobs. The map
  lays itself out left to right and keeps everything in view as it grows; drag boxes to arrange them
  (remembered in the browser), Auto layout resets.
- **Inspector:** click a box (details, lines, recent activity), a line (message count, recent messages) or
  any event (the job: state, attempts, steps with errors, result, input, events).
- **Status tiles:** running, waiting, succeeded, dead, workers. **Events panel** with filters (jobs, steps,
  workers, map, problems). Works on a phone.
- **Token:** Helios asks for `ARGUS_WORKER_TOKEN` once (if set) and keeps it in the browser; password login
  comes in C11.
- **API:** `/events` gains `component=` (sent or received by) and `newest=true`; the WebSocket accepts
  `component=` too.
- **Dev:** `helios/` source, `dev.ps1 helios` to rebuild, CI type-checks and builds it.
- **Tests:** 135.

## 0.3.0 (2026-09-28)

Core step C5: live events and the growing map.

- **Live event stream:** WebSocket `/ws/events` pushes every change (jobs, steps, workers, components) as
  it happens, typically within 0.1 s. Filters by kind (`kinds=job.,worker.`) or job. Reconnect with
  `since=<seq>` and missed events are replayed from the database, so a viewer never loses one. A viewer
  that falls too far behind is disconnected (and simply reconnects), so it can never slow Argus down.
- **The map grows by itself:** new `edges` table (migration 0002). The first time two components talk, an
  `edge.added` event appears; plugins get a box the first time a job is queued for them. `GET /map` returns
  boxes, lines, job counts per plugin and the `seq` to stream from.
- **More events:** `worker.online` / `worker.offline`, `component.added`, `edge.added`.
- **New endpoints:** `/events` (paged history), `/map`, `/registry`, public `/status`.
- **Live home page:** status cards and a live event feed. With a token set, open `/?token=<token>`.
- **`argus-events`** (`dev.ps1 events`): watch events live in a terminal, with colours, filters and
  auto-reconnect.
- **Retention:** events older than `events.retention_days` (default 90) are deleted in small chunks; the
  map's edges are kept.
- **Stability:** event IDs stay in order even if the clock steps back; Argus stops at once even with viewers
  connected (a closed viewer used to linger for up to 20 s and delay shutdown); a hung test now fails after
  120 s instead of hanging the run.
- **Dev:** `dev.ps1` reinstalls dependencies automatically when they change (this version adds
  `websockets`).
- **Tests:** 133.

## 0.2.0 (2026-09-28)

Core step C4: workers.

- **Worker protocol (HTTP):** register, long-poll claim (only jobs whose plugin the worker has), start,
  heartbeat, step reports, succeed, fail, wait; plus submit, list, counts, events, cancel, rerun and resume
  for apps and Helios. Errors are plain: 404 unknown job, 409 lease lost, 429 queue full.
- **Token auth:** set `ARGUS_WORKER_TOKEN` in `.env` and every `/jobs` and `/workers` call needs
  `Authorization: Bearer <token>`. `/`, `/health` and `/version` stay public.
- **`argus-worker`:** pulls jobs and runs plugin workflows. Steps are checkpoints (`ctx.step`), so a job
  retried after a crash skips finished steps; `ctx.idempotency_key` for side effects; `ctx.wait()` parks a
  job; `PermanentError` sends it straight to dead. Heartbeats in the background, stops a job the moment its
  lease is lost, rides out Argus restarts (retries for ~30 s). Ctrl+C exits at once when idle, or after the
  current job.
- **Built-in demo plugin** (`demo.echo`, `demo.sleep`, `demo.fail`) to try it without writing code.
- **Registry:** workers and the core show up as components (the boxes Helios will draw); silent workers are
  marked offline by the watchdog. The home page lists connected workers.
- **Fix:** a write whose caller gave up (a cancelled request) could crash the database writer thread. Such
  writes are now skipped and the writer can no longer die from one bad batch.
- **Tests:** 118. New: the full API contract, auth, end-to-end jobs through a real server, wait and resume,
  and killing a worker mid-step so another worker finishes the job without redoing the finished step.

## 0.1.1 (2026-09-28)

- Home page at `/`: status, database, watchdog, job counts and links (it returned "Not Found" before).
- Logs no longer include uvicorn's `color_message` field with terminal colour codes.

## 0.1.0 (2026-09-28)

First code: the core foundation (Argus Core Design steps C1-C3).

- **C1 skeleton:** `argusd` daemon with `--check`; `argus.yaml` + `.env` config validated at startup with clear
  error messages (exit code 2); JSON-lines logs to stdout and a rotating file; `/health` and `/version`.
  Starts in about 0.5 s.
- **C2 store:** SQLite in WAL mode with one writer thread and group commit (each write in its own savepoint,
  so one failing write never undoes others); numbered migrations; the ten core tables; refuses a newer
  database.
- **C3 jobs:** the job state machine (queued, leased, running, waiting, retry, succeeded, dead, cancelled)
  with every transition checked and logged as an event in the same transaction; capability-based claiming
  with priorities; leases and heartbeats; retries with backoff and dead-letter; step checkpoints; dedupe
  keys; per-plugin queue limits; watchdog that requeues jobs from dead workers.
- **Tests:** 104 tests, including every allowed and forbidden state transition, 1,000 concurrent writes, and
  killing the process mid-write with no half-written jobs.
