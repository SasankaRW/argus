# Phone delivery, piece 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (chosen: native). Steps use checkbox (`- [ ]`) syntax.

**Goal:** A phone message only counts as delivered once Android can really show it; when Android blocks Argus, the message waits and Helios says so.

**Architecture:** The app already holds every message while *all* Argus notifications are off (`Notifier.show` throws, the inbox loop doesn't acknowledge). The gap is a single channel turned off (`IMPORTANCE_NONE`): `nm.notify` drops it silently and the app acknowledges it. A small pure check decides "can show / why not"; the app passes the reason with its next inbox poll; Argus keeps it on the phone app's node; Helios's Phone box turns amber with "Android is blocking Argus notifications" in the attention row (also shown inside the phone app, which is Helios).

**Tech Stack:** Kotlin (Android app, JUnit on the JVM), Python 3.12 (core, pytest), React/TS (helios, vitest).

**Spec:** `docs/superpowers/specs/2026-11-20-live-map-and-stock-alerts-design.md`, part 2 and settled item 5.

## Global Constraints

- Held messages stay `pending` in the outbox, so they expire after `notify.keep_hours` as today; shown on the first poll once Android allows them; never shown twice (ids already acknowledged are not fetched again).
- Copy, sentence case, no full stops on labels: attention row "Android is blocking Argus notifications", detail = the reason, Phone box status "notifications blocked".
- App version 1.8 (versionCode 8); release notes; the APK copied to `G:\Projects\argus-packages\argus-phone-app-1.8.apk`.
- Helios change: rebuild and commit `core/argus/helios_dist`. Commits `type(scope): msg`, first line ≤ 72 characters.

## Review Focus

1. The app was blocked and then allowed again: the reason must clear on the next poll (send `blocked=` empty), or the amber box sticks forever. Test in Task 2 (an empty `blocked` clears it) and Task 1 (`blockReason` returns null when fine).
2. The phone goes offline while blocked: an old reason must not show once the app hasn't polled for 90 s (same window as `connected()`). Test in Task 2.
3. A reason with odd characters (a channel name with `&`) must reach Argus intact: URL-encode it in the app (Task 1), cap it at 120 characters in Argus (Task 2).

---

### Task 1: the app knows when Android won't show a message

**Files:**
- Create: `android/app/src/main/java/dev/argus/companion/ShowCheck.kt`
- Modify: `android/app/src/main/java/dev/argus/companion/Notifier.kt` (`show`), `WorkerService.kt` (inbox loop), `android/app/build.gradle*` (version)
- Test: `android/app/src/test/java/dev/argus/companion/ShowCheckTest.kt`

**Interfaces:**
- Produces: `object ShowCheck { fun blockReason(appEnabled: Boolean, channelName: String, channelImportance: Int?): String? }` — null when Android will show it; `"notifications are off for Argus"` when `!appEnabled`; `"the \"<channelName>\" notifications are off"` when `channelImportance == 0` (`NotificationManager.IMPORTANCE_NONE`); null when the channel doesn't exist yet (it is created first) or has any other importance.
- `Notifier.show` throws `IllegalStateException(reason)` when `blockReason` is non-null (reading `nm.getNotificationChannel(channel(priority))?.importance` after `channels(ctx)`).
- The inbox loop remembers the last reason (`@Volatile var blocked: String?`), sets it from the exception, clears it when a message shows, and adds `&blocked=${Uri.encode(blocked ?: "")}` to every `GET /phone/inbox`.

- [ ] Step 1: `ShowCheckTest`: fine → null; app off → "notifications are off for Argus"; channel importance 0 → `the "Approvals and warnings" notifications are off`; channel missing (null) → null.
- [ ] Step 2: `cd android; .\gradlew.bat testDebugUnitTest --tests "*ShowCheckTest"` → FAIL (unresolved ShowCheck)
- [ ] Step 3: implement `ShowCheck`, use it in `Notifier.show`, the inbox loop change, version 1.8 / 8
- [ ] Step 4: same command → PASS; `.\gradlew.bat assembleRelease` (or the build the repo's android/README.md names) → BUILD SUCCESSFUL
- [ ] Step 5: commit `fix(android): hold messages Android would hide, and say why`

### Task 2: Argus keeps the reason on the phone app's box

**Files:**
- Modify: `core/argus/outbox.py` (`PhoneInbox`: `blocked: str`, `seen(device, blocked=None)`; `inbox_fetch(..., blocked: str | None = None)`), `core/argus/api/routes/phone.py` (`blocked: str = Query("", max_length=300)` passed through), `core/argus/registry.py` (`phone-app` node gets `"blocked": inbox.blocked if inbox.connected() else ""`)
- Test: the existing phone inbox test file (`grep -ln "phone/inbox" tests`)

- [ ] Step 1: tests: `test_the_app_says_android_is_blocking` (GET `/phone/inbox?blocked=the%20%22Approvals%20and%20warnings%22%20notifications%20are%20off` → `/map` phone-app node `blocked` equals that text, capped at 120); `test_a_clear_poll_clears_it` (then `blocked=` → `""`); `test_an_old_reason_goes_with_the_connection` (clock past 90 s → `""`).
- [ ] Step 2: run them → FAIL
- [ ] Step 3: implement
- [ ] Step 4: run them and `tests/` for outbox/phone → PASS
- [ ] Step 5: commit `feat(phone): Argus hears when Android blocks its notifications`

### Task 3: Helios shows it

**Files:**
- Modify: `helios/src/mapGraph.ts` (phone card), `helios/src/api.ts` (`MapNode.blocked?: string`)
- Test: `helios/src/mapGraph.test.ts`

- [ ] Step 1: test: `phone-app` node `{ blocked: "the \"Approvals and warnings\" notifications are off" }` → phone card `tone "warn"`, `status "notifications blocked"`, `problem "Android is blocking Argus notifications"`, `detail` = the reason; offline still wins (offline + blocked → "offline").
- [ ] Step 2: `npx vitest run src/mapGraph.test.ts` → FAIL
- [ ] Step 3: implement (after the offline and failed checks, before "waiting")
- [ ] Step 4: `npx vitest run`, `npm run typecheck` → PASS
- [ ] Step 5: commit `feat(helios): the Phone box says when Android blocks Argus`

### Task 4: ship

- [ ] `.\scripts\dev.ps1 helios`; commit `core/argus/helios_dist`; CHANGELOG under Unreleased ("Phone app 1.8: a message Android would hide stays queued, and Helios's Phone box says Android is blocking Argus notifications"); copy the release APK to `argus-packages\argus-phone-app-1.8.apk`; `dev.ps1 test`; `dev.ps1 pr`.
