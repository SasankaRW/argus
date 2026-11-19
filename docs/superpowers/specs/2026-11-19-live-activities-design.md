# Live activities (design)

Sas asked on 2026-11-18 for running jobs, transfers, timers and the workout rest timer to show as live pills on the PC island and in a phone notification. Sas was asleep, so the calls below are Claude's and are marked as such. Nothing visible merges before Sas sees the previews.

## What exists (survey 2026-11-19)
- **Send transfers.**
  - In memory in `core/argus/send_live.py`, served at `GET /send/live`.
  - A `send.live` hub event wakes listeners.
  - The island long-polls `GET /events` and then polls `/send/live` every 0.5 s while something moves. Its `TRANSFER` mode (300×54, `draw_transfer`) already shows one transfer.
- **Jobs.** The hub emits `job.<state>` and `step.<state>` events. Steps carry the step name and index, but there's no percent anywhere.
- **Phone timer.** The `phone` plugin's `timer` workflow runs on the phone and fires `AlarmClock.ACTION_SET_TIMER`. Argus knows only `{minutes}`, and the phone's Clock app shows its own countdown.
- **Phone app.**
  - Long-polls over HTTP from a foreground service: `/phone/inbox`, worker claim, and Send. No FCM.
  - compileSdk 36. `SendNotes` already uses `Notification.ProgressStyle`, the Android 16 promoted Live Update style, with a fallback.
- **Workout tracker.** It has no connection to Argus. Its rest timer is `www/js/timer.js`, as pure timestamp functions.

## Decided (Claude's calls)

### 1. One activity model in Argus core
- A new in-memory registry, `core/argus/activities.py`, like `send_live.py`. It's never stored: activities are live state, and a restart clears them.
- Activity shape: `{id, kind: "transfer"|"job"|"timer"|"custom", title, detail, icon, progress: 0..1|null, ends_at: unix|null, started_at, device|null, priority}`.
- **Sources:**
  - **Transfers.** They're mapped from `send_live`, as one activity per moving transfer. They're derived on read, so nothing is duplicated.
  - **Jobs.** A job becomes an activity once it has run for 10 s, so quick jobs never flash. The detail is the current step name and "step n". There's no progress value; the pill shows an indeterminate shimmer line. It ends on any terminal `job.*` event. It's fed from the store's existing event emit points (no polling).
  - **Timers.** When a `phone.timer` job succeeds, Argus adds an activity that ends at finish time + minutes and drops itself at that time. Also `POST /activities/timer {minutes, label}` for Ari or Helios to start a PC-side timer. When it ends, a hub event `activity.done` fires, and the island chimes, using its existing notification sound if one exists.
  - **Custom (for the workout app and plugins).** `PUT /activities/custom/{id}` and `DELETE /activities/custom/{id}`, with any non-revoked token (named tokens since #181). A custom activity expires on its own after `ends_at + 60 s`, or after 10 min without an update when it has no `ends_at`. The workout tracker's rest timer uses this; see section 5.
- **API.**
  - `GET /activities` returns `{version, items}`. `?after=<version>&wait=25` long-polls, so clients sleep until something changes.
  - Every change bumps `version` and emits one `activity.changed` hub event, coalesced to at most 4 per second.

### 2. Island (PC)
- **A new `live` mode,** used when Ari is idle and any activity exists. Ari's phases still win, as today. The existing `transfer` mode becomes the transfer case of `live`, keeping its look.
- **Pill size 300×54,** the same as transfer. Left: the activity's icon in its tone colour. Middle: the title on one line, and the detail plus a thin line underneath, which is progress, an indeterminate shimmer for jobs, or a draining line for timers. Right: the countdown (`4:32`), the percent, or the elapsed time. The tone tokens are the island's existing ones; no new colours.
- **More than one activity:** the most urgent shows, with a small `+2` chip at the right edge. Order:
  1. a timer under 60 s from its end;
  2. transfers;
  3. timers;
  4. jobs;
  5. custom.

  Ties go to the newest. Hovering expands the island to a stacked list of up to 4 rows. Clicking a row opens the matching Helios page: Transfers, the job's run, or nothing for a timer.
- **Feed:** the island adds `activity.changed` to its existing `/events` long-poll kinds and fetches `/activities` on wake. While a timer or progress is visible, it redraws once a second, because only the countdown text changes. The 0.5 s Send polling is replaced by this feed. Reduced motion means no shimmer.
- **A timer ending** gives a short pulse of the pill and a chime. Then the activity goes away after 5 s.

### 3. Phone notification
- **One ongoing notification,** "Live", on a new channel "Live activities" with LOW importance. It shows the top activity only, ranked as on the island but skipping:
  - transfers the phone is part of, because SendNotes already shows them;
  - `phone.timer` timers, because the Clock app already shows them.
- **Layout:** `Notification.ProgressStyle` on API 36, with segment progress or indeterminate. Timers use `setUsesChronometer(true)` with `setChronometerCountDown(true)` and `setWhen(ends_at)`, so the system ticks the countdown with no app wake-ups. Older versions use the same fallback as SendNotes.
- **Feed:** a small loop in `WorkerService` long-polls `GET /activities?after=v&wait=25`. It's the same pattern as the inbox, so no new connection type and no FCM. When nothing qualifies, the notification is cancelled.
- **Tap** opens Helios at the matching page.

### 4. Helios
- No new page tonight. The Transfers page already shows transfers, and the map shows jobs.

### 5. Workout rest timer (follow-up, its own PR in workout-tracker)
- The workout app gets an optional "Argus" setting: Argus URL plus a named token. With it set, starting or adjusting a rest timer sends `PUT /activities/custom/workout-rest` with the title "Rest", the exercise name and `ends_at`. Finishing or skipping sends DELETE. A failed call is ignored and never blocks the app.
- The settings row needs Sas's preview, in the Sonoma look, so it isn't built tonight. The core API above supports it.

## Non-goals
- History of activities.
- Activities on the Windows lock screen.
- Percent progress for jobs. Plugins can publish custom activities with progress later.

## Tests
- Core: the registry (ranking, expiry, coalesced events, long-poll wake), job promotion after 10 s, timer from `phone.timer`, custom-activity auth (named token OK, no token 401), and transfers derived from send_live.
- Island: the pure helpers (ranking, words, countdown format) plus offscreen previews.
- Android: JVM test of the ranking and skip rules.

## Shipping
- Core and island: one PR. Android: one PR, with the app going to version 1.7.
- Held for Sas's preview approval. Island previews are rendered with `ari_popup --preview`; the phone notification is described, plus an emulator-free mock image.
