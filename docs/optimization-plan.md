# Argus fix and optimization plan

Written 2026-11-01 from a look at the live Argus (v0.7.0): jobs, dead letters, schedules, backups and the
`argus` / `worker-laptop` logs. Week numbers: 3,738 jobs, 14 dead, about 26 minutes saved.

Order: Phase 0 is config only, so do it first. Phase 1 fixes failures. Phase 2 cuts wasted work. Phase 3 is quality.

---

## Phase 0: the money advisor (config only)

**Problem.** Every `advisor.*` job dies with `put Life Hub's API_KEY in Argus's .env as LIFEHUB_API_KEY`.

**Cause.** The key *is* in `G:\Projects\argus\.env`, but the advisor never reads that file:

- `plugins/advisor/plugin.yaml` says `runs_on: any`, so the laptop worker (`worker-calypso`) picks the jobs up.
- Workers read secrets from **their own machine's** `.env` (`core/argus/worker/plugins.py`: "read from this
  machine's .env"), and the laptop's `/opt/argus/.env` has no `LIFEHUB_API_KEY`.
- Even with the key copied there, the advisor would call `http://127.0.0.1:8081`, which is the laptop itself, while
  Life Hub runs on the PC. That's the other error in the dead list: `Life Hub isn't answering at http://127.0.0.1:8081`.
- `deploy/linux/argus.laptop.yaml` already pins `lifehub: desktop` but leaves out `advisor`, which uses Life Hub
  just as much.

**Fix.**

1. Laptop, `/opt/argus/argus.yaml`, under `plugins:`:
   `runs_on: {web: desktop, lifehub: desktop, tracker: laptop, advisor: desktop}`
2. Same change in `deploy/linux/argus.laptop.yaml`, so a reinstall keeps it.
3. `sudo systemctl restart argusd`.
4. Check: press "Check wishlist prices now". The job should run on the PC's worker (not `worker-calypso`) and succeed.

Don't copy the key to the laptop: the advisor needs the PC's Life Hub anyway.

**Also check:** any other `runs_on: any` plugin that calls a service on `127.0.0.1` that only exists on the PC.
`watchers` looks fine (it calls web pages).

---

## Phase 1: failures

### 1.1 "Service isn't answering" should be retried

**Problem.** When Tracker (8282) or Life Hub (8081) is down, plugins raise `PermanentError`, so each job dies on
the first try. On Oct 23, `fixer.scan` and `fixer.ask` died every 2 minutes for as long as Tracker was off.

**Where.** `raise PermanentError(f"... isn't answering at {base}")` in:
`plugins/advisor/plugin.py:45`, `plugins/diary/plugin.py:47`, `plugins/fixer/plugin.py:681`,
`plugins/lifehub/plugin.py:28`, `plugins/tracker/plugin.py:30`, `plugins/web/plugin.py:91`.

**Fix.**

- Raise a retryable error for connection failures (OSError). Keep `PermanentError` for a wrong key (401/403).
- Add a simple circuit breaker in the scheduler: after 3 connection failures in a row for a plugin, skip its
  scheduled runs for 15 minutes and send **one** "Tracker is down" note to the evening summary. Clear it on the
  first success.
- `homelab.check` should also check that Life Hub, Tracker and SearXNG answer, so you hear about a stopped
  container from one place.

**Check.** Stop Tracker's container: one entry in the summary, no wall of dead jobs, and fixer resumes by
itself when Tracker is back.

### 1.2 Vault "busy" retries finish before the note stops being busy

**Problem.** An inbox note ("Fix DT Server Connection", Oct 24) never got filed. The job failed with
`Busy: ... trying again later`, but all 3 attempts ran within about 70 seconds.

**Cause.** `RECENT = 120` seconds (`plugins/vault/plugin.py:47`), but `jobs.backoff_seconds` is
`[10, 60, 600]` and `max_attempts: 3`, so the retries happen at +10 s and +70 s. Both are still inside the
120-second busy window, so the third failure kills the job.

**Fix.** Treat `Busy` as "run again in RECENT + 30 seconds" without using up an attempt (a delayed requeue or a
`WaitSignal`), not as an ordinary failure. Then re-file the stuck note: run `vault.tidy`, or move the file out
of `00 Inbox` and back in.

**Check.** A test that edits the target note during `write`: the job should requeue about 150 seconds later and
succeed.

### 1.3 Restarts end in timeout tracebacks

**Problem.** Argus restarted 3 times today. The two restarts in the log (09:40 and 15:36) each produced 13
`CancelledError: Task cancelled, timeout graceful shutdown exceeded` tracebacks.

**Cause.** `timeout_graceful_shutdown=5` (`core/argus/daemon.py:63`), but the workers' long-poll claim
(`api/app.py` around line 2146, `await asyncio.sleep(CLAIM_POLL)`) and the `/ws/events` subscribers
(around line 2036, `await sub.queue.get()`) don't notice the shutdown. Uvicorn waits 5 seconds and then kills them.

**Fix.** Add one `shutting_down` asyncio.Event, set in the lifespan shutdown. The claim loop returns 204 when it
is set, and the events loop wakes on it and closes the socket with code 1001.

**Check.** `systemctl restart argusd`: no tracebacks, and workers reconnect within a few seconds (they already
retry).

### 1.4 Alert on the same failure repeating

The advisor failed for a week and nothing told you. Add a rule to the summary and weekly review: when the same
error text kills a job 3 or more times, show it once, with the count and the error's own fix hint.

---

## Phase 2: wasted work

All of this week's time saved came from screenshot-renamer, downloads-organizer, vault and pc-apps. The rest
was mostly empty runs:

| Schedule | Now | Runs/week | Proposed |
| --- | --- | --- | --- |
| `fixer.scan` | every 2 min | ~5,000 | merge with `ask`; every 10 min, plus run at once when Tracker sends a ticket-changed webhook |
| `fixer.ask` | every 2 min | ~5,000 | (merged into the above) |
| `eyes.scan` | every 5 min | ~2,000 | every 15 min |
| `diary.tidy` | every 10 min | ~1,000 | every 30 min, plus run when Life Hub saves a diary entry, if it can send a webhook |
| `vault.tidy` | every 10 min | ~1,000 | every 30 min (the file watch already catches new inbox files; tidy is the safety net) |

(The weekly review counted 3,738 jobs, fewer than the cron maths, because jobs only run while the PC worker is
on.)

Steps:

1. Change the crons in each `plugin.yaml` (and override them in `argus.yaml` if you'd rather not touch plugins).
2. Fixer: one `tick` workflow that does `scan` then `ask` in one job, so there are half the jobs.
3. Empty runs: when a scheduled job's result is empty (`{"planned": null}` and so on), keep a counter instead of
   a history row, or prune them after 24 hours. The job list becomes readable again.
4. Optional: Tracker webhook into Argus's existing `triggers.webhooks`, so a new `ai-fix` ticket is picked up
   in seconds, not minutes, while the poll drops to every 10 minutes.

**Expected:** about 80% fewer jobs, less SQLite churn, and a job history where failures stand out.

---

## Phase 3: quality

### 3.1 Vault topic choice

The Oct 24 note about fixing someone's DT server connection was placed under
*Software Development / QA Server Debug Logging*, linked to *Plugin Code* and *Approval Levels*, by the T1
model with `sure: true`. Options:

- Use T2 for `place` when the note is short (under 300 characters, so there's little context), or when the best
  topic's similarity is under a threshold.
- Don't trust the model's own `sure`. Require that at least one tag or word overlaps with the chosen note's
  tags or title, or ask.

### 3.2 Backups

`backup.keep: 5` gives only about 5 days of history. Change it to 7 daily + 4 weekly + 3 monthly (or simply
`keep: 14` for now). The `copy_to: G:/ArgusBackups` copy uses the same rule.

### 3.3 Weekly review suggestions

It gave none this week. Add: buttons pressed by hand 3 or more times that failed every time (the price check
was), and dead-letter groups from 1.4.

---

## A correction to the earlier review

`worker-calypso-now` is **not** a duplicate worker. It's the laptop's fast lane
(`core/argus/worker/runner.py:278`, `Worker(..., f"{main.id}-now", ...)`). Both of them signing in every 5
minutes is expected. Nothing to change.

---

## Order and size

| # | Item | Size | Risk |
| --- | --- | --- | --- |
| 0 | advisor `runs_on: desktop` | 2 lines of config | none |
| 1.1 | retry on "not answering" + circuit breaker | small, plus a scheduler change | low |
| 1.2 | vault Busy delay | small | low |
| 1.3 | clean shutdown | small | low |
| 1.4 | repeat-failure alert | small | none |
| 2 | slower schedules, fixer merge, empty-run pruning | medium | low (behaviour stays the same, just slower) |
| 3.x | vault model, backups, weekly review | small each | none |

Each step: change, run `pytest`, restart, check the matching job in Helios.

---

## Found while fixing Phase 0 (2026-11-01)

**Phase 0 result.** Life Hub and Tracker moved to the laptop (`calypso:8081`, `calypso:8282`); the PC's containers
stopped on Oct 23. The advisor now runs on the PC (`plugins.runs_on.advisor: desktop` in the laptop's argus.yaml)
with its Life Hub address set to the laptop in Helios. `advisor.prices` succeeds.

**Wrong Tracker location.** The laptop's argus.yaml still says `tracker: desktop` with `127.0.0.1:8282`. Check
that fixer and the tracker plugin point at `calypso:8282` (Helios settings), and that fixer's "succeeded"
runs reach Tracker at all.

**Watchers took the ISBN as the price.** On `jeyabookcentre.com/item/64632-dune-1` the page text reads
`EAN13 9780441172719\nLKR 3,996.00`. `PRICE`'s "number then currency" alternative
(`plugins/watchers/plugin.py:73`) allows `\s?` between them, which matches the newline, so it read
`9780441172719 LKR`. That number went to Life Hub as the Dune book's price history.
Fix: allow only a space or non-breaking space there (`[  ]?`), not a newline; prefer currency-first
matches; ignore numbers of 10 or more digits with no separators. Add this page's text as a test. Then remove the
bad price point in Life Hub and re-add the watch so its starting price is LKR 3,996.

**Done (uncommitted, 2026-11-01):** watchers read prices on the same line only and skip 10+ digit codes, read
stock (JSON-LD / og:availability, else "out of stock" etc. right after the price), alert on back-in-stock and
out-of-stock, and ignore a hundredfold price jump as a misread. Life Hub got an `item_links` table (extra shop
links per wishlist item, with each link's last price and stock) and a Shops section in the item sheet; the
advisor watches every link, sends stock and `source: watch:<shop>`, skips impossible prices, stops watches for
removed links, and retries unwatchable pages daily.
