# A livelier map, stock alerts that reach you, and your apps on the map (design, 2026-11-20)

Sas asked for the desktop landing screen (the live map) to be more visually impressive, and found that a wishlist
restock alert never reached them. They approved the preview (https://claude.ai/artifact/GdU8LUsVkw61s7m4neRDBQ,
version 6, source kept next to this file as `2026-11-20-live-map-preview.html`) and this list of five pieces, built in
this order, each its own PR (`dev.ps1 pr` / `merge local`). House rules apply: Material 3 Expressive in Helios,
Sonoma in Life Hub, sentence case, no gradients, no hover lift, all motion off under reduced motion, commits
`type(scope): msg`.

## Why

- The map is what makes Argus Argus, but at rest only a dash moves. Sas wants it to read as a system that is working.
- On 2026-11-16 18:24 the Watchers plugin saw "Dune book is back in stock at LKR 4,495.5" and queued a phone message
  (job `01M7J6R1JX2F8ZM1F3J04QYNNZ`, outbox `01M7J6R54B0343D4ZYEG2B36BV`). The phone app collected and acknowledged it
  11 s later (`state: delivered`), but Sas never saw it. Ari said nothing either: `ari.speak_up` defaults to false and
  isn't set, and even when on, a notice is dropped (not kept) when Sas is idle, outside speak hours, or Ari spoke in
  the last 10 minutes.

## Approaches considered

For the map: (A) bring the existing map to life, (B) add a hero band above it, (C) a slim hero plus (A). Sas picked
**A**: it keeps the layout Sas likes and only touches the map's drawing. For Hush and the workout tracker on the map:
they are phone-only with no server, so the only status would be "installed, last opened", which is noise; left out.

---

## 1. The live map (Helios desktop, `LevelCanvas.tsx`, `MapView.tsx`, `livemap.css`)

What Sas approved in the preview, on the real data:

- **Travelling jobs.** A busy line stays solid but quieter (opacity about .42); glowing dots (a bright head and three
  fading trail dots, the line's colour) travel along it. A dot is spawned from real activity: a `job.*` event (or a
  message between two boxes the map already counts to mark a line busy) on that line, at most one every 600 ms per
  line and at most 12 on screen. Speed about 300 px/s in canvas units, eased in and out.
- **A job's route.** When a job's events show it moving on (app → Argus → machine → plugin → model), the next dot
  starts on the next line when the previous one arrives. Where the route isn't known, single-line dots are fine.
- **Boxes react.** The box a dot reaches flashes once (an inset ring that fades, about 750 ms; the icon tile pops to
  1.1). No movement of the box itself.
- **The core.** The Argus box gets the 9-lobe scalloped badge behind its icon, turning once every 24 s, and a ring that
  breathes outward every 3.4 s. Its status line reads "N running · N today", and the numbers tick when they change.
- **The opening.** On first load of the map page (not on every data refresh), boxes rise in column by column (560 ms,
  emphasized decelerate, 90 ms per column) and solid lines draw themselves; dashed lines and badges fade in after.
- **News travelling home.** A phone message of priority high or above for the plugin on the map (for example a
  Watchers stock alert) runs as an amber dot against the lines: plugin → machine → Argus, then splits to the Phone box
  and (when the message came from a wishlist watch) the Life Hub box. On arrival, an amber banner at the top right of
  the map says the message ("Dune book is back in stock" / "LKR 4,495.5 at jeyabookcentre.com · told your phone and
  Ari") for about 5 s.
- **What just happened.** A strip along the bottom of the map holds the last three events, newest first ("A job
  finished on Calypso", "Notifications sorted 3 from your phone"), with a quiet "4 s ago". Older chips glide over
  (FLIP); the fourth fades out of the row's flow so nothing jumps. Only events a person would recognise go in, in
  plain words (a small mapping table, tested).
- **Pointing at a box** dims what it isn't joined to (exists today); dots on dimmed lines dim too.
- **Reduced motion and hidden tabs.** No dots, no spinning, no breathing, no opening; the strip updates without
  sliding. Nothing animates while the tab is hidden (`usePauseWhenHidden` already exists).
- **Now vs before.** The "Command" layout and the phone's sideways map get the same drawing; the phone's upright map
  gets the dots and flashes only (no banner, no strip: it has its own sheets).

## 2. Phone app: only "delivered" once Android shows it (`android/.../WorkerService.kt`, `Notifier.kt`)

- Before showing, check `NotificationManager.areNotificationsEnabled()` and that the message's channel isn't set to
  `IMPORTANCE_NONE`. If it can't be shown, don't acknowledge it: keep it pending, and show it in the app's own list
  with a line "Android is blocking Argus notifications: turn them on" that opens the app's notification settings.
- Argus's phone box already turns amber for messages waiting; that now also covers this case.
- Version bump, release notes, the APK in `argus-packages` as usual.

## 3. Ari tells you now, or when you're back (`core/argus/ari/ari_listen.py`, config)

- **Speak up by default for what matters.** `ari.speak_up` becomes true by default, and a switch for it goes in Helios
  Settings (Ari section), with the speak hours next to it.
- **Now:** a notice arriving while Sas is at the PC (idle under 5 min, not paused, within speak hours, not mid
  conversation) is said straight away, as today.
- **Later:** otherwise the notice is kept (newest last, at most 5, each dropped after 24 h). The 10-minute gap between
  spoken notices also keeps, rather than drops. When Sas comes back (input after 5+ min idle, or Ari resumed, or speak
  hours start) and nothing is playing, Ari says one short line: one notice as itself ("While you were away: Dune book
  came back in stock at jeyabookcentre."), several as one sentence naming up to three ("While you were away: Dune book
  came back in stock, and two jobs failed.").
- Kept notices survive Ari restarting only if cheap (a small JSON file next to Ari's other state); otherwise memory
  is enough. Builder's call.
- **In the Ari chat.** A stock or price notice also goes into Ari's conversation list as a message from Ari, so it can
  be read on the phone later.

## 4. Life Hub banner (Life Hub repo, `G:\Projects\lifehub`, Sonoma look)

- Life Hub records each stock or price change it is told about: a small `link_events` table (item uid, url, shop,
  kind `back_in_stock` | `out_of_stock` | `price_drop`, price, currency, at, dismissed_at). `_link_status` in
  `advisor_api.py` writes one when `in_stock` flips (false → true is the one Sas asked for; true → false is recorded
  but not bannered), and when a price drops.
- `GET /api/advisor/news` returns the undismissed `back_in_stock` and `price_drop` events; `POST
  /api/advisor/news/{id}/dismiss` dismisses one. Opening the item also dismisses its events.
- A banner at the top of the wishlist and of Home: "Back in stock: Dune book · LKR 4,495.5 at jeyabookcentre",
  with Open and Dismiss. Several stack into one banner ("2 wishlist updates") that expands. It stays until opened or
  dismissed (decided for Sas: it can't be missed by glancing past it once).
- Sonoma tones: green for back in stock, blue for a price drop.

## 5. Your apps on the map (Argus core + `mapGraph.ts`)

- Argus checks each app it already talks to through a plugin with a `url` setting: **Life Hub** (`/api/health`) and
  **Tracker** (`/api/health`), every 2 minutes, with a 5 s timeout, remembering up/down, since when, and the last
  error. Cashly lives inside Life Hub and isn't a separate box.
- Each becomes an `app` node on the map (the Apps column already draws registry apps) with one line of status:
  Life Hub "N back in stock" (from part 4's news count) or "All synced"; Tracker "N open issues". Its line to Argus
  is quiet when up, broken (red, ✕ badge) when down for 2+ checks, and it then tops the attention row: "Life Hub
  isn't answering · since 14:20 · Argus keeps trying every minute".
- Hush and the workout tracker stay off the map.

---

## Goals, non-goals, anti-goals

- Goals: the map visibly alive on real data; a restock reaches Sas on the phone, by voice, and in Life Hub; app health
  at a glance.
- Non-goals: a redesign of the map's layout; new pages; Hush and workout tracker status; Life Hub's price watch page
  itself (Sas wants it improved; that's its own brainstorm next).
- Anti-goals: noisy stats; motion that never rests (the dots only run on real activity, so a quiet Argus is still);
  gradients or hover lift; Ari talking over Sas or announcing stale news.

## Settled after the builder check (Claude's calls, for Sas to check)

1. **Life Hub hears within minutes.** Today `advisor.prices` runs every 6 hours (00:41, 06:41 ...) and Watchers checks
   every 3, so Life Hub could lag hours behind the phone. When a Watchers check changes a watch whose name starts
   with "Wishlist: ", it enqueues `advisor.prices` (deduped) so Life Hub gets the flip in the same minute. A first
   reading (`in_stock` was None) is never a restock.
2. **"From a wishlist watch"** is an explicit tag: Watchers adds `wishlist` to the message's tags for watches named
   "Wishlist: …". The map, Ari's chat and the Life Hub branch key on that tag, never on text.
3. **App health source:** the `url` setting of the `lifehub` and `tracker` plugins. A box shows only when that plugin
   is enabled and has a url; otherwise there is no box (not "unknown").
4. **Ari's "back":** idle is the existing `idle_seconds()` (Windows last input). Back = input after 5+ minutes idle,
   or un-paused, or speak hours starting. The 10-minute gap still applies to the catch-up line. If audio can't play,
   the notices stay kept.
5. **Phone app, held messages:** they stay pending in the outbox (so they expire after `notify.keep_hours` as today)
   and are shown on the first poll after notifications are allowed again; shown ids are remembered so nothing is
   shown twice.
6. **Ari chat:** only messages tagged `wishlist` go into the chat, whether or not Ari spoke them and whatever
   `speak_up` says. The phone notification is separate; no other dedupe needed.
7. **Job routes:** chained routes only where events name the next hop; single-line dots otherwise.
8. **Order:** Life Hub's part 4 ships (on Calypso, as Life Hub deploys today) before part 5 reads its news count;
   part 5 shows "All synced" if the news endpoint isn't there yet. The APK goes in `argus-packages` with part 2.

## Left to the builder

The exact event-to-line mapping for dots (from what `buildLevels` already uses to mark a line busy); the strip's
wording table; whether kept notices persist; test layout. Tests: the pure parts (dot spawning rules, strip wording,
route chaining, notice keeping and phrasing, Life Hub event writing, app health thresholds) get unit tests; the
drawing gets the existing Helios tests plus a manual check on Calypso. Every Helios change rebuilds and commits
`core/argus/helios_dist`.
