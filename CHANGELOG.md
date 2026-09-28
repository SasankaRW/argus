# Changelog

All notable changes to Argus. Versions follow `MAJOR.MINOR.PATCH`.

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
