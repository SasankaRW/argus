# Four fixes found on 2026-11-18 (design, for review before any code)

Sas asked to fix the four problems found while shipping Send's live transfers and file opening, with specialist
review of the solution before building. Each fix ships as its own PR (`dev.ps1 pr` / `merge local`), small and
independently revertible. House rules apply: Material 3 Expressive in Helios, sentence case, no gradients, a preview
before a visible UI change, commits `type(scope): msg` with no attribution lines.

---

## 1. Stop Helios shipping without its rebuilt bundle

**Problem.** Calypso serves Helios from the committed build `core/argus/helios_dist/` (vite `outDir`); its updater
never runs npm. On 2026-11-18 a rail change (#176) merged as source only, so Calypso served the old UI until #177.
`dev.ps1 merge local` runs ruff, pytest and `npm run typecheck`, but nothing checks the bundle matches the source.

**Design.**
- `merge local` (and the GitHub CI workflow, same check) runs `npm run build` into a temporary directory
  (`vite build --outDir <tmp> --emptyOutDir`) and compares it with `core/argus/helios_dist/` file by file (names and
  bytes). Vite builds are deterministic for the same inputs and lockfile, so any difference means the committed
  bundle is stale.
- On a difference: fail with "Helios source changed but core/argus/helios_dist wasn't rebuilt: run
  .\scripts\dev.ps1 helios, commit, pr again", listing up to 10 differing files.
- Skipped (with a printed note) when npm isn't installed, as typecheck is today.
- `dev.ps1 helios` already rebuilds; no change there.

**Risks to check.** Determinism across machines (Windows vs the Ubuntu CI runner: line endings in `index.html`,
hash stability, absolute paths in source maps; we don't emit source maps). Build time added to merge (~1 s).

---

## 2. Phone app: shared-file copies are never deleted

**Problem.** `SendHub.enqueue` copies every shared file up to 64 MB into `filesDir/outbox/<share-id>/<n>` so the send
doesn't depend on the sharing app's permission lasting. Nothing ever deletes these copies. Since 1.5 the Transfers
page also opens "sent" files from these copies (`SendQueue.mine_files`, `PhoneMine`).

**Design.**
- A cleanup pass `SendHub.cleanOutbox(ctx)` run when the Send service starts and at most once a day after that:
  delete `outbox/<id>/` when the share is no longer in the queue (done, cancelled or failed for good) **and** either
  - it is older than 30 days, or
  - the outbox as a whole is over 500 MB (then oldest first until under 400 MB).
- Never delete a copy whose share is still queued, sending, verifying or held (it may still be needed to send).
- After deleting, the Transfers page shows those sent files as "moved or deleted" (`here: false`) - already handled.
- Settings in the app's Send screen: none for now (sane defaults); the numbers are constants in one place.

**Risks to check.** Held shares (with Calypso, waiting for the target) must keep their copy until delivered. Clock
changes. A share id reused? (UUIDs: no.)

---

## 3. One worker token gives full control of Argus everywhere

**Problem.** Every client uses the same `ARGUS_WORKER_TOKEN`: workers, the PC and Calypso Send agents, the Ari popup,
the phone app (which also hands it to Helios in its WebView, stored in localStorage), Helios in desktop browsers,
the MCP server. One guard (`token_ok` in `api/app.py`) accepts only that token. A lost phone or a leaked browser
profile means rotating the token on every device by hand.

**Design (revocable named tokens; scopes only where they are cheap and safe).**
- A new table `api_token(id, name, scope, hash, created_at, last_used_at, revoked_at)`; tokens are random 32-byte
  values shown once; only a SHA-256 hash is stored.
- Scopes:
  - `full` - everything the worker token can do (Helios on the phone and on computers, the Ari popup).
  - `send` - only `/send/*` routes (Send agents: register, events, live, devices, history).
- `token_ok` accepts the worker token (unchanged, always `full`) or any non-revoked named token; a `send` token on a
  non-`/send/` route gets 403. `last_used_at` is updated at most once a minute per token.
- Managing tokens: `argus token new <name> --scope full|send`, `argus token list`, `argus token revoke <name>` (CLI
  on Calypso), plus `POST/GET/DELETE /tokens` guarded by the worker token only (not by named tokens), and a small
  "Devices and tokens" list in Helios Settings with Revoke (hold to confirm) - preview first.
- Roll-out: nothing breaks; the worker token keeps working. Then the phone app gets its own `full` token
  ("my phone"), each Send agent a `send` token. A lost phone = revoke "my phone".

**Out of scope.** Fine-grained scopes for Helios pages; OAuth; expiry.

**Risks to check.** Routes outside `guarded` that call `token_ok` directly (WebSocket `/ws` with `?token=`, MCP, the
`/send/files` style query tokens, Helios `?token=` adoption). The hot path cost of a DB lookup per request (cache the
hash set in memory, refresh on change). Timing-safe comparison. The Send agent's own routes on 8610 compare against
`agent.argus_token` - must accept its `send` token.

---

## 4. Calypso's updates are invisible

**Problem.** `deploy/linux/argus-update.sh` runs every 5 minutes: fetch, back up, pull, pip install, `--check`,
restart, health check, roll back on failure. Its result is only in `journalctl -u argus-update`. On 2026-11-18 there
was no way to see from the PC whether Calypso had picked up a merge, or why it hadn't.

**Design.**
- The updater writes one small JSON file after each attempt that changed something:
  `/opt/argus/data/last-update.json` = `{at, from, to, subject, result: "updated"|"rolled_back"|"failed", detail}`
  (atomic write: temp + mv). Quiet runs (nothing new) don't touch it.
- argusd: `/version` adds `commit_subject`, `started_at`, `behind` (commits on origin/main not yet running, from the
  updater's last fetch: the updater also writes `{checked_at, origin}` to `last-check.json` every run) and
  `last_update` (the JSON above).
- Helios: the status chip on the desktop top bar ("Up 48 s") opens a small card: running commit + subject, started,
  last update (result and when), "1 merge waiting" when behind, and "rolled back" in the error colour when the last
  attempt failed. The phone's More > Settings shows the same lines. Preview first.
- `dev.ps1 merge` already waits for nothing; add `dev.ps1 deployed` that polls `/version` until the merged commit is
  running (or prints the last-update result).

**Risks to check.** File permissions (the updater runs as the argus user; argusd reads). Rolled back = the old commit
is running again; the card must make that obvious.

---

# Decisions after specialist review (2026-11-18) - these replace the sections above where they differ

## 1. Build check (reviewer: CI already has it)
- `.github/workflows/ci.yml` job `helios` already builds in place and fails on `git status --porcelain -- core/argus/helios_dist`.
  The gap is only `dev.ps1 merge local`, which ran ruff, pytest and `npm run typecheck`.
- `merge local` now runs, in helios/: `npm ci`, `npm run typecheck`, `npm test`, `npm run build`, then fails if
  `git status --porcelain -- core/argus/helios_dist` is not empty (message: run `.\scripts\dev.ps1 helios`, commit, pr
  again). It always restores the tree afterwards (`git checkout -- core/argus/helios_dist` + `git clean -fd -- core/argus/helios_dist`).
- npm missing: fail (not skip) when the branch's diff against main touches `helios/`; otherwise print a loud note.
- No change to the GitHub workflow. Verified: a Windows build is byte-identical to the committed bundle.

## 2. Phone outbox cleanup (reviewer: protect by state, not by "row gone")
- Keep `outbox/<id>/` while its share row is QUEUED, SENDING, VERIFYING, HELD or FAILED (FAILED can be retried).
- Delete it when the row is CANCELLED, or there is no row (DONE or orphan), and the folder's newest file mtime is
  older than 30 days. A FAILED share older than 30 days: `queue.remove(id)` and delete the copy together.
- Over 500 MB in total: delete deletable folders oldest first until under 400 MB (never protected states).
- Grace: skip any folder whose newest mtime is under 1 hour old (enqueue creates the folder before the row). Read the
  share rows after listing folders. Future mtimes (clock changes) = keep.
- Run from `SendEngine.loop()` (already off the main thread): once on the first pass after start, then when 24 h
  have passed since `lastClean` (SharedPreferences). Synchronized against `SendQueue.add`.
- `enqueue`: delete the folder when nothing was queued or a copy failed half way.
- `mine_files` rows stay; Transfers shows such files as "moved or deleted".
- A pure decision function (state, newest mtime, now -> keep/delete) with JVM unit tests.

## 3. Tokens (reviewer: right-size to the goal)
- Minimal version: `api_token(id, name UNIQUE, hash, created_at, last_used_at, revoked_at)` (migration 0022), one
  scope (full). `token_ok` accepts the worker token or a non-revoked named token (SHA-256 of the presented value,
  looked up in an in-memory dict refreshed on create/revoke and every 30 s; `hmac.compare_digest`).
- Covers every entry point: `auth`/`guarded`, MCP auth, `/ws/events` (query token + header), the approvals decide
  path. Open WebSockets re-check their token every 60 s and close if it was revoked.
- Manage: `POST /tokens {name}` -> the token once, `GET /tokens`, `DELETE /tokens/{name}`; these accept **only the
  worker token**. `argus token new|list|revoke` CLI calls those routes using argus.yaml's URL and worker token (so it
  runs from the PC too).
- Never logged: add named tokens to the log redaction and URL scrubbing.
- Roll-out: the phone gets its own token ("my phone"); Send agents, workers and the PC keep the worker token. No
  `send` scope and no Helios UI for now. Revoking a lost phone = `argus token revoke "my phone"`.

## 4. Update visibility (reviewer: record every run, not only successes)
- Files in argusd's real data dir (`/opt/argus/data`, resolved from the database path, not hardcoded):
  `last-check.json` `{checked_at, origin, behind, error?}` written every run right after `git fetch` (also on fetch
  failure), and `last-update.json` `{at, from, to, subject, result: updated|rolled_back|failed, step, detail}`.
  Atomic: temp file in data/ then `mv`.
- Updater: `flock -n data/.update.lock`; a `step=` variable before each stage; an early `trap fail_early ERR`
  (fetch, rev-parse) replaced by `rollback` after the reset; rollback writes its JSON first; backup failure noted in
  `detail`; `behind` = `git rev-list --count HEAD..origin/main`.
- argusd: a new token-guarded `GET /deploy` (not `/version`, which is public) = running commit + subject,
  started_at (from uptime), `last_check`, `last_update`, and `stale` when last_check is older than 15 min ("the
  updater isn't running"). `last_update.result` is authoritative over the startup `commit`.
- `dev.ps1 deployed`: poll `/deploy` until the merged commit (`git rev-parse origin/main`) runs, up to 8 min, or print
  the last update's result; `ship` ends with it. Fix the stale "ship.ps1" comment in api/app.py.
- Helios: the desktop "Up …" chip opens a small card (running commit + subject, started, last update and result,
  "1 merge waiting", "rolled back" / "updater not running" in the error colour). On the phone the same lines in
  More > Settings. Preview before merge.
