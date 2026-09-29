# Changelog

All notable changes to Argus. Versions follow `MAJOR.MINOR.PATCH`. New entries go under **Unreleased**;
`dev.ps1 release` turns that section into the next version.

## Unreleased

- downloads-organizer: videos always go to Downloads\Videos and audio to Downloads\Audio, decided by file type in code (rules `by_extension:`), no model; Tidy folders also moves such files out of the wrong folders. Optional `destinations:` keeps a category outside Downloads.
- **screenshot-renamer names the window in front, not the background:** the vision model goes first (it sees which window the screenshot is about), with the text near the middle as a hint. Every word of a name must be on screen in the front window or be a plain describing word (folder, error, login page); made-up words or words from other windows are sent back, and if nothing passes the name stays as it is.
- **downloads-organizer: one folder per kind.** Media is now Audio; "Tidy folders" folds Pictures into Images, Media and Music into Audio, Miscellaneous into Misc (each move can be undone).
- **Rules in Helios:** plugins with a rules file (downloads-organizer) get an "Edit sorting rules" button: a YAML editor with Save, Reset to default, and the examples learned from Wrong folder. Edits apply from the next job, no restart; rules that make no sense stop the job with a message that says what to fix.
- **Share to Argus:** Helios installs on the phone (Add to Home screen) and appears in its share menu. Share a photo, PDF, link or text, pick where it goes, Send. Also on the new Share page in Helios (Add files).
- downloads-organizer takes shares ("Save to PC"): files are saved to Downloads and sorted at once; links become .url shortcuts, text a .txt.
- Plugins declare `share:` targets; jobs read shared files with `ctx.shared(name)`. API: `GET /share/targets`, `POST /shares`, `PUT /shares/{id}/files`, `POST /shares/{id}/send`; shares are kept 7 days (`share.keep_days`, `share.max_mb`).
- **`dev.ps1 up` without extra windows:** Ollama, argusd and the worker run in the background and "up" prints one tidy summary. `dev.ps1 status` shows what runs; `dev.ps1 logs` follows every log in one coloured terminal. If something fails to start, its last log lines are shown right away.
- **Helios Logs page:** argusd, worker and Ollama logs in one live view, with source tabs, Warnings / Errors filters, search and pause (`GET /logs`, `GET /logs/{name}`).
- The worker can write a rotated log file (`--log-file`); a process running in the background logs only to its file.
- **screenshot-renamer plugin:** new screenshots get names like "2026-09-28 cashly login bug.png". Text on screen (Tesseract) goes to T1; otherwise the vision model V1 (qwen2.5vl:7b) looks at the picture. Names are checked in code; only default names are touched; Undo in Helios.
- Models: `ctx.llm(..., images=[...])` sends pictures to a vision tier; tiers outside the chain (like V1) can be listed in a manifest.
- New `plugins` extra (send2trash, Pillow, pytesseract), installed by dev.ps1.
## 0.7.0 (2026-09-29)

- **Helios Runs page:** every job, newest first, filtered by plugin and state, with a one-line summary; click one for its details.
- **Undo and Wrong buttons:** a job's Changes list shows each file it moved. Undo puts one back; Wrong folder moves it to the folder you pick and keeps that as an example the models see next time (downloads-organizer).
- `GET /jobs?plugin=&before=`; `GET /jobs/{id}/changes`, `POST /jobs/{id}/changes/{event}/undo` and `/wrong`.
- A plugin switched to live (or with new settings) takes effect on the next job, even if the worker registered before argusd restarted.
- downloads-organizer: a dry-run result now says "would_move" and "nothing was moved" instead of "moved".
- Plugin trace events no longer draw stray "file" and "plugin" boxes on the map (old ones are removed).
- **First plugin: downloads-organizer.** Sorts new files in Downloads into category folders: siblings stay
  together, a matching existing folder wins without a model, T1 picks the category (T2 when T1's answer isn't a
  real category), extension fallback last. Starts from the folder watch, a nightly sweep, or its Sort now button.
  Dry-run until listed under `plugins.live`. Rules in `plugins/downloads-organizer/rules.yaml`.
- Helios: a plugin's box shows dry-run or live, what starts it, and its buttons.
- Folder triggers accept `~` in paths.
- **Plugins as folders (C10):** a plugin is `plugins/<id>/plugin.yaml` + `plugin.py`. argusd checks manifests
  (bad ones are listed under `GET /plugins`, never fatal), wires their triggers (schedules, folder watches,
  webhooks, Run now) and applies a per-plugin Claude cap. Workers load the plugins they can run (`--cap desktop
  --cap gpu`, or `ARGUS_WORKER_CAPS`); each job's `ctx` only reaches the manifest's folders, hosts, secrets and
  model tiers (`PermissionDenied` otherwise). New plugins run in dry-run until listed under `plugins.live`; every
  file change is an event; moves never overwrite; deletes go to the Recycle Bin.
- **Power manager (simulated):** logs `power.would_wake` when GPU/desktop work waits with no PC worker, and
  `power.would_shutdown` after `power.idle_minutes` (20) without PC work. Its state shows on the Helios map.
- Fixed a rare hang on shutdown in the event stream and outbox (Python 3.11 `wait_for` cancel race).
- **Map lines are curves again,** routed around the boxes (ELK splines) instead of right angles.
- **Review fixes (29 Sep):**
  - Security: `.env` saved with a BOM (Notepad) no longer turns auth off; an empty `ARGUS_WORKER_TOKEN` counts as
    unset, and argusd refuses to listen beyond localhost without one; secrets are hidden from config printouts;
    tokens in URLs are masked in logs; `/lite` escapes event text; the phone page only links to http(s) and sends a
    strict Content-Security-Policy; approval links must be http(s).
  - Jobs: a worker is never starved by a pile of jobs it cannot run (no top-200 cut; plugin filter in SQL);
    waiting for an approval no longer uses up a retry attempt; Re-run with an active duplicate answers 409 instead
    of 500; cancelling or dead-lettering a job closes its pending approvals; idle long-polls only read.
  - Durability: SQLite commits with `synchronous=FULL` (the laptop has no battery); writes queued during shutdown
    fail cleanly instead of hanging.
  - Models: Claude gets its playbook through stdin, never the Windows command line; a Claude timeout ends the whole
    process tree (claude.cmd -> node) so a worker can't hang; every call needs its own permit (retries count
    against the Claude cap); a half-open breaker lets exactly one trial call through.
  - Worker: an argusd outage while reporting no longer stops the worker.
  - Approvals: edited fields keep their type and an edited amount must be valid (formatted in code); a late
    answer counts as expired and sticks; ntfy mode can carry a write-only token (`NTFY_REPLY_WRITE_TOKEN`).
  - Helios: a changed token shows the login instead of "Connecting" forever; line counts are no longer counted
    twice after a refresh; the map re-renders only while something glows (idle costs nothing); on a phone one
    finger scrolls the page and taps don't drag boxes; the inspector scrolls into view on narrow screens; line
    history looks further back; dev-server proxy covers /models, /approvals, /outbox.
  - Tooling: `up`/`down` check pid and start time (Windows reuses pids) and record each start at once; `merge`
    refuses when local commits aren't on GitHub and waits for checks to appear; CI fails when `helios_dist` is
    stale; `release` prints recovery steps if the push fails; README and CONTRIBUTING match the workflow.
  - Indexes on events by component and jobs by date (migration 0005); `/status` cached for 2 s.
  - 188 tests.
- **Plugin Guide 0.5:** the plugin plan in five waves (24 plugins).

Core step C9: scheduler, triggers and dispatcher.

- **Schedules:** cron jobs under `schedules:` in argus.yaml (`0 7 * * *`, `@daily`; local time). After downtime a
  schedule runs once, not once per missed slot; a run still queued is merged with the next. Helios' Scheduler box
  lists them with the next run and a Run now button (`GET /schedules`, `POST /schedules/{id}/run`).
- **Windows:** `windows: {night: "01:00-06:00"}`; a job or schedule with `window: night` only starts inside it.
- **Folder triggers:** `triggers.folders` names a folder on a worker's machine; that worker watches it and reports
  each file once it has stopped changing (`settle_seconds`), skipping temporary downloads. Files are remembered by
  content, so copies, re-downloads and restarts never process a file twice; when the plugin's queue is full the
  watcher holds the rest back and offers them later (`POST /triggers/file`).
- **Webhooks:** `POST /hooks/<name>`, signed with the hook's secret from .env: a timestamped HMAC (replays refused)
  or GitHub's `X-Hub-Signature-256`. Retried deliveries are merged.
- **Dispatcher:** priorities (interactive 90, resumed after approval 80, scheduled 50, batch 20); one job per plugin
  at a time by default (`jobs.plugin_concurrency`, per-plugin `jobs.concurrency`); one GPU job at a time, and GPU
  jobs for the model already loaded go first, so Ollama swaps models rarely (`model` on a job or schedule).
- Database migration 0006. Core Design 0.8. 201 tests, including the gate: 100 mixed jobs with at most 2 model
  swaps, and a flood of 500 files merged and limited.
- **Devices in their own column:** the PC's worker, the phone and apps always sit in the first column of the
  Helios map under a plain "Devices" heading; the rest is laid out by traffic as before, models last. Lines follow
  ELK's routes around the boxes instead of cutting across them, and the map is
  more compact. A box you drag gets a plain curve until Auto layout.
- **Phone on the map:** a Phone box shows online / offline (green / red) and since when, like the PC's worker.
- **Approval notifications say "Approve / Reject need Tailscale"** when the buttons go over Tailscale.
- **Phone buttons go over Tailscale, and Argus catches up when the phone reconnects:** Approve / Reject go
  straight to Argus at `approvals.public_url`, so knowing the ntfy topic is not enough to approve anything. Set
  `approvals.phone` to the phone's Tailscale name and Argus checks `tailscale status` every 30 s; when the phone
  comes back online and something is still waiting, it sends one message (the card again with fresh buttons,
  or "N approvals waiting"), at most once per 15 minutes. `/health` shows it under `phone`.
- **Optional ntfy mode** (`approvals.buttons: ntfy`, for your own ntfy server behind a login): the buttons post
  to a private reply topic on the ntfy server so they work anywhere; Argus keeps one streaming connection to it,
  decides with the signed one-time token and replies "Approved: ..." (or "Already approved: ...").

## 0.6.0 (2026-09-28)

Core step C8: approvals, the outbox and ntfy.

- **`ctx.approve()` for plugins:** a step asks you and the job waits without holding a worker. When you answer,
  the job goes back to the queue ahead of scheduled work, the step runs again and `ctx.approve` returns your
  `Decision` (truthy when approved; `.fields` has the values as approved, edits included). Three card types:
  `entry` (editable fields), `batch` (items; Argus adds up the count and total in code) and `draft`. Asking twice
  from a retried step returns the same approval, never a second one.
- **Approve from the phone:** the ntfy message has Approve / Reject / Open buttons carrying a signed one-time
  token (`approvals.public_url` must be the address your phone reaches Argus on). Open shows a small page with
  the card. A used or forged link does nothing.
- **Reminder and expiry:** one reminder after 24 hours; after 7 days the approval counts as "no" and the job
  carries on.
- **Outbox:** messages are written in the same transaction as the change that causes them and sent by a
  background task, with retries and backoff while ntfy is down. Nothing is lost when argusd restarts, and a
  message is never queued twice.
- **ntfy:** approvals, dead jobs and `ctx.notify()` reach your phone. Set `NTFY_TOPIC` in `.env`.
- **Helios:** Approvals and ntfy boxes on the map; Approve / Reject (with edits) in the job and Approvals panels;
  an Approvals filter in the event list; plugin boxes show waiting jobs.
- **API:** `POST /jobs/{id}/approvals`, `POST /jobs/{id}/notify`, `GET /approvals`, `POST /approvals/{id}/decide`,
  `GET /a/{id}` (phone page), `GET /outbox`, `POST /outbox/test`. Database migration 0004.
- **dev.ps1:** `ntfy` (test message), `approval` (a pretend bill that waits for you), `approve` / `approve no`.
- **Docs:** Core Design 0.7, Plugin Guide 0.4.
- 173 tests (16 new, including the gate: phone Approve to finished job in under 1 s, and no duplicate
  notification after a worker or argusd crash).

## 0.5.2 (2026-09-28)

- **One command to start everything:** `dev.ps1 up` (or double-click `scripts\up.cmd`) starts Ollama, argusd
  and a worker if they are not running, waits until each is ready, and opens Helios. `dev.ps1 down` (or
  `scripts\down.cmd`) stops what `up` started; `down all` stops Ollama too.

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
