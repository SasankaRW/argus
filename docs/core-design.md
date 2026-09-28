# Argus Core Design

Sep 28, 2026 · @Sasanka

**Version 0.5** · draft, becomes 1.0 when M1 starts · history at the end · all versions in the Argus Docs Index

## Goals

Argus core is one small daemon (`argusd`) built around a durable job queue in SQLite. Every piece of work is a job, and every job survives crashes, restarts, power cuts and the PC being off. Plugins and workers are clients of the same API, so adding a plugin, a worker or a second machine never changes the core. This design is milestone M1 of the Argus Development Plan.

| Goal | What it means here | How we check it |
| --- | --- | --- |
| **Super stable** | No lost jobs, no half-done side effects, no silent failures. A crash or power cut loses at most the step that was running, and it retries. | Fault-injection tests (kill workers, pull the network, fill the disk) plus a 48-hour soak test with zero lost or stuck jobs |
| **Scalable** | Adding plugins, job types, workers or machines is configuration, not a rewrite. Comfortable up to 10,000 jobs a day and 50 plugins. | Load test at 10× expected volume |
| **Super smooth** | Helios and the API never wait on a model. Clicks answer in under 100 ms, live updates arrive in under 1 second, and the GPU never swaps models needlessly. | Latency budgets checked in CI and shown in Helios |

**Non-goals:** multiple users, high availability across machines, a distributed queue, Kubernetes. One person, one laptop, one or two workers. The design keeps doors open (Postgres, more workers) without paying for them now.

## Process architecture

`argusd` never runs plugin code or model calls itself. It stores jobs, hands them out and records what happened, so it stays small, fast and hard to crash.

&#91;embedded content: D9 v1 · process architecture · argusd, SQLite, workers\]

- **argusd** is one asyncio process: API, scheduler, dispatcher, watchdog, event bus, outbox, approvals, and config and registry. systemd restarts it if it dies; it is back in under 2 seconds with nothing lost, because all state is in SQLite.
- **SQLite in WAL mode** is the single source of truth. One writer task owns all writes (a write queue), so there is never a "database is locked" error; reads are concurrent.
- **Workers** run plugin steps in separate processes and pull work over the same HTTP API, whether they sit on the laptop or the PC. A plugin that crashes or hangs kills only its worker, never `argusd`.
- **Capabilities** route work: the laptop pool says `cpu, claude`, the PC runner says `gpu, files:pc`. A job asks for capabilities and only matching workers can claim it.

## Job lifecycle

Every job moves through one small state machine, and every state change is a single SQLite transaction plus an event. Nothing is ever "half moved".

&#91;embedded content: D10 v1 · job state machine · 7 states\]

- **Leases:** a worker claims a job for 60 seconds and renews it with a heartbeat every 15 seconds. If a worker dies, the watchdog sees the lease expire and puts the job back in the queue.
- **Steps are checkpoints:** each finished step saves its output. A retried job resumes after the last finished step instead of starting over, so a bill already filed is never filed twice.
- **Waiting is not failing:** a job waiting for your approval, a free GPU or the PC to wake holds no worker and times out on its own clock (approvals: 24 hours, then a reminder).
- **Retries** use backoff (10 seconds, 1 minute, 10 minutes). After 3 tries the job goes to the dead-letter list in Helios with its full history, and you can re-run it with one click.
- **Cancel** works from any state; the running step finishes or is stopped at its next safe point.

## Data model

Ten tables in one SQLite file. Every table has `created_at` and `updated_at`. IDs are ULIDs, which sort by time and are safe to create on any machine.

| Table | Holds | Key columns |
| --- | --- | --- |
| `jobs` | one row per unit of work | `id`, `plugin`, `workflow`, `state`, `priority`, `needs` (capabilities), `dedupe_key`, `attempt`, `max_attempts`, `run_after`, `lease_owner`, `lease_until`, `input` (JSON) |
| `steps` | checkpoints inside a job | `job_id`, `name`, `index`, `state`, `tier_used`, `output` (JSON), `error`, `started_at`, `finished_at` |
| `events` | append-only log that feeds the map and Helios | `id`, `job_id`, `step`, `kind`, `from_component`, `to_component`, `data` (JSON, small), `at` |
| `approvals` | things waiting for you | `id`, `job_id`, `type` (entry, batch, draft), `payload`, `state`, `token_hash`, `expires_at`, `decided_by`, `decided_at` |
| `outbox` | side effects to send exactly once (ntfy, webhooks out) | `id`, `kind`, `payload`, `state`, `attempts`, `next_try_at` |
| `workers` | who is connected and what they can do | `id`, `host`, `capabilities`, `version`, `last_seen`, `state` |
| `components` | the registry the map draws boxes from | `id`, `kind` (core, plugin, model, app, worker), `label`, `group`, `meta`, `first_seen` |
| `schedules` | cron and window triggers | `id`, `plugin`, `cron`, `next_run_at`, `last_run_at`, `enabled` |
| `plugin_state` | each plugin's own small key-value store | `plugin`, `key`, `value` |
| `settings` | config values changed from Helios | `key`, `value`, `updated_at` |

**Indexes that keep it fast:** `jobs(state, priority, run_after)` for claiming, `jobs(dedupe_key)` unique for queued and running jobs, `events(at)` and `events(job_id)`.

**Retention:** events are kept for 90 days, then rolled up into daily totals per component (the map's line thickness uses those). Finished jobs and steps are kept for a year. A nightly `VACUUM` runs in the backup window.

**Migrations:** numbered SQL files in `core/db/migrations/`, applied at startup inside a transaction. `argusd` refuses to start if the database is newer than the code.

## Stability

Stability comes from a few rules applied everywhere, not from special cases.

1. **State lives only in SQLite.** No in-memory queues or caches that matter. Kill `argusd` at any moment and a restart picks up exactly where it was.
2. **One writer.** All writes go through one async writer task with a short queue, so writes never collide. The WAL keeps reads fast while it writes.
3. **Leases and heartbeats** on every running job (see Job lifecycle). A dead worker never strands a job.
4. **Idempotent steps.** Each side effect carries an idempotency key (`job_id:step`). The desktop runner keeps an operations log, so repeating "move this file" after a crash is a no-op.
5. **Outbox for side effects.** ntfy messages and outgoing calls are written to the outbox in the same transaction as the state change, then sent by the outbox task. A message is never lost or sent twice.
6. **Timeouts everywhere.** Model calls 120 seconds, `claude -p` 300 seconds, file jobs 60 seconds, HTTP 10 seconds. Nothing waits forever.
7. **Circuit breakers** on Ollama, `claude -p` and the PC. After 3 failures in a row, calls stop for 60 seconds, dependent jobs move to Waiting, and Helios shows the part in red. There are no retry storms.
8. **Backpressure.** Each plugin has a queue limit (default 100) and a concurrency limit (default 1). Duplicate triggers merge through `dedupe_key` (for example a file's hash), so a folder full of files never floods the queue.
9. **Validated input and config.** Config, plugin manifests and every model answer are checked with Pydantic before use. Bad config stops startup with a clear message; a bad model answer fails only that step.
10. **Health and self-repair.** `/health` checks the database, the writer, the scheduler and the outbox. systemd restarts `argusd` if the check fails twice. The watchdog also reclaims expired leases and approvals.

### What happens when something breaks

| Failure | What Argus does | What you see |
| --- | --- | --- |
| `argusd` crashes | systemd restarts it in about 2 seconds; running jobs keep going on their workers and report back | nothing, or a short gap on the map |
| Worker dies mid-step | lease expires after 60 seconds; the job is requeued and resumes at that step | the step retried once in the run history |
| PC is off or asleep | GPU and PC-file jobs wait; the power manager wakes the PC in the next window or on demand | jobs marked Waiting for PC |
| Ollama errors or hangs | timeout, then circuit breaker; jobs wait and retry | Ollama box red on the map |
| Model answer fails its check | retry, then escalate a tier, then retry; after 3 tries, dead-letter | the escalation on the map; dead-letter entry if it fails |
| `claude -p` over its cap or logged out | T3 steps fall back to T2 plus your approval | a Claude warning in Helios |
| Power cut | on restart, leases expire and jobs resume from their last step; the outbox resends anything unsent | a gap in the timeline |
| Disk nearly full | at 90% full, new batch jobs pause and old events are compacted | a warning notification |
| Corrupt or bad database migration | refuses to start; restore the last backup (last 5 kept) | clear error, one restore command |

## Scalability

Argus grows by adding plugins and workers, never by changing core. Four design choices make that possible.

- **Pull-based workers with capabilities.** Workers announce what they can do (`cpu`, `gpu`, `claude`, `files:pc`, `files:laptop`) and pull jobs that match. A second GPU machine, or a CPU-only worker for OCR, joins by starting a runner with a token. No core change.
- **One protocol for everything.** Local workers, the PC runner and future machines all use the same HTTP API. There is no special path that only works on one machine.
- **A storage layer behind an interface.** Core talks to a `JobStore`, not to SQLite directly. If volume ever outgrows SQLite, a Postgres store drops in behind the same interface.
- **Plugins are data plus code in their own process.** Core loads manifests, not Python. A new plugin cannot slow `argusd` down or crash it.

| Dimension | Comfortable with this design | First change if it grows beyond that |
| --- | --- | --- |
| Jobs per day | 10,000 (expected: 50–500) | Postgres `JobStore` |
| Events per day | 500,000 with compaction | shorter raw retention |
| Plugins | 50 | none needed |
| Workers | 5 machines | none needed |
| Helios clients | a few devices | none needed |
| GPUs | 1 now; each extra GPU is one more worker with `gpu` | none needed |

## Smoothness

Smooth means the system never makes you wait for something slow. Every slow thing (a model, the PC waking up, Claude) happens in a job, and the API only ever reads and writes the database.

| Action | Budget | How |
| --- | --- | --- |
| Any API call from Helios or an app | under 100 ms | never waits on a model; returns a job ID straight away |
| Map and event updates | under 1 s after they happen | event bus pushes over WebSocket; the map batches updates every 250 ms |
| Approve on the phone | under 1 s to confirmed | approval written, job resumed, reply sent |
| Share or Ask from the phone (PC on) | first answer in 2–5 s | interactive priority jumps the queue; the 7B model stays loaded |
| `argusd` start | under 2 s | no heavy imports; migrations are quick |

**Priorities:** interactive (Share, Ask, anything you're waiting on) comes first, then jobs resuming after an approval, then scheduled jobs, then night batch work. Within a priority, oldest first.

**GPU scheduling:** the dispatcher gives out one GPU lease at a time and groups queued work by model, so it runs all 7B jobs, then switches to 14B once. Loading a model takes seconds; swapping on every job would waste most of the night window. The last-used model stays loaded for 10 minutes (`keep_alive`) for fast follow-ups.

**No notification spam:** notifications are rate-limited and merged ("3 bills filed" instead of three pings). Only approvals and failures notify right away; the rest waits for the evening summary.

## Modules and interfaces

Each module owns one job and talks to the others only through a small interface, so each can be tested alone with fakes.

| Module | Owns | Main interface |
| --- | --- | --- |
| `db` | SQLite connection, single writer, migrations | `Store.write(fn)`, `Store.read(sql)` |
| `jobs` | job and step state machine, leases, retries, dedupe | `JobStore.enqueue()`, `claim(worker, caps)`, `heartbeat()`, `complete_step()`, `fail()`, `wait()` |
| `scheduler` | cron, night window, folder and webhook triggers | `Scheduler.tick()` → enqueues jobs |
| `dispatcher` | priorities, capability matching, GPU leases grouped by model | `Dispatcher.next_for(worker)` |
| `models` | tiers, output schemas, checks, escalation, `claude -p` wrapper, circuit breakers | `ModelRouter.ask(tier, playbook, input, schema)` |
| `approvals` | approval records, signed one-time tokens, expiry | `Approvals.create()`, `decide(token, answer)` |
| `events` | append-only log, pub/sub, WebSocket fan-out, compaction | `Events.emit()`, `subscribe()` |
| `outbox` | ntfy and other outgoing messages, exactly once | `Outbox.add()`, sender task |
| `registry` | components for the map, workers and capabilities | `Registry.upsert()`, `list()` |
| `plugins` | manifest loading and validation, step lookup | `PluginHost.load_all()`, `step(plugin, name)` |
| `power` | wake, idle detection, shutdown handshake (simulated on the PC) | `Power.ensure_awake()`, `maybe_shutdown()` |
| `api` | REST and WebSocket routes, auth, tokens | FastAPI app |
| `worker` | the process that pulls jobs and runs steps with `ctx` | `argus-worker --caps cpu,claude` |

**Stack:** Python 3.12, asyncio, FastAPI, Pydantic v2, `aiosqlite`, `httpx` (Ollama and the API), `croniter`, `structlog` (JSON logs), `ulid`. No ORM: plain SQL in the `jobs` and `db` modules, so the queries that matter stay visible and fast.

**Config:** one `argus.yaml` (hosts, folders, limits, windows) plus `.env` for secrets, validated at startup. The same code runs on the PC and the laptop; only this file differs.

## Testing

Stability is proven by breaking things on purpose, not assumed.

| Layer | What | Runs |
| --- | --- | --- |
| Unit | every module with fakes; the job state machine is tested against every allowed and forbidden transition | every commit, under 30 s |
| Contract | a fake worker and fake Ollama and `claude` talk to a real `argusd` over HTTP | every commit |
| Fault injection | kill a worker mid-step, kill `argusd` mid-write, drop the network, make Ollama hang or return garbage, fill the disk, expire leases early | every commit (fast set), nightly (full set) |
| Soak | 48 hours, 1,000 mixed jobs per hour, random faults every few minutes | before each milestone gate |
| Load | 10× expected volume for one hour; checks the latency budgets | before each milestone gate |
| Real models | the M1 gate workflow against real Ollama and `claude -p` | at each gate |

**Pass rule for the core:** after the soak test, every job is Succeeded or Dead-letter, with no stuck or duplicated jobs, no side effect done twice, and every latency budget met. The test report goes into the milestone's gate notes.

## Build steps

Twelve steps, each about one evening session with Claude writing the code and you running and checking it on the PC. At about 4 sessions a week that is 3 weeks, so M1 in the development plan becomes weeks 2–4. Each step ends in a working, tested state, and the next step starts only when its check passes.

| # | Step | Done when |
| --- | --- | --- |
| C1 | Skeleton: repo layout, `argus.yaml` + `.env` loading with validation, JSON logs, `/health` | `argusd` starts in under 2 s; a bad config stops it with a clear message |
| C2 | Store: migrations, the ten tables, single writer | 1,000 concurrent writes with no lock errors; restart keeps everything |
| C3 | Jobs: state machine, enqueue, claim, lease, heartbeat, retry with backoff, dedupe, dead-letter, watchdog | every allowed and forbidden transition is tested; killing `argusd` mid-write loses nothing |
| C4 | Worker: `argus-worker` pulls jobs by capability, runs steps with `ctx`, checkpoints, idempotency keys | killing a worker mid-step resumes the job at that step; no side effect runs twice |
| C5 | Events, registry and WebSocket | events reach a test client in under 1 s; components appear when they first register |
| C6 | Helios v0, part 1: live map drawn from the registry and events, in the mockup style | a test job visibly moves across the map |
| C7 | Models: Ollama client, tiers, schemas and checks, escalation, circuit breakers, `claude -p` wrapper with tools off and a daily cap | hang and garbage tests pass; real T1 → T2 escalation works |
| C8 | Approvals + outbox + ntfy (one-time signed tokens) | approving from the phone resumes the job in under 1 s; no duplicate notification after a crash |
| C9 | Scheduler + dispatcher: cron, night window, folder and webhook triggers, priorities, GPU leases grouped by model, backpressure | 100 mixed jobs run with at most 2 model swaps; a flood of 500 files is merged and limited |
| C10 | Power manager (simulated on the PC) and the plugin host (manifest loading, folder allowlist) | a test plugin runs from its folder; "would shut down" is logged after idle |
| C11 | Helios v0, part 2: approvals inbox, event list, password login | the M1 gate flow is fully usable from the phone |
| C12 | Fault suite, 48-hour soak, load test | the pass rule in Testing holds; this is the M1 gate |

After C12 the core is frozen as API version 1.0: later milestones add plugins and Helios views, and core changes only through a versioned, tested change.

**Progress:** C1–C6 are done in Argus `0.4.0` (repo `G:\Projects\argus`, commit `c423b35`, tag `v0.4.0`). C6 added Helios v0 at `/helios` (and `/`): a React + React Flow + ELK live map in the mockup style that grows as components talk, with a dot per message along each line, busy and offline states, an inspector for boxes, lines and jobs, status tiles, a filtered event list and a phone layout. The built copy ships inside the Python package, so running it needs no Node.js. 135 tests pass. Next: C7, models, tiers, escalation and `claude -p`.

## Document history

| Version | Date | Change |
| --- | --- | --- |
| 0.5 | 2026-09-28 | C6 (Helios v0 live map) built and tested in Argus 0.4.0 |
| 0.4 | 2026-09-28 | C5 (live events, growing map) built and tested in Argus 0.3.0 |
| 0.3 | 2026-09-28 | C4 (workers) built and tested in Argus 0.2.0 |
| 0.2 | 2026-09-28 | C1–C3 built and tested in Argus 0.1.0 |
| 0.1 | 2026-09-28 | Created (D9, D10) |
