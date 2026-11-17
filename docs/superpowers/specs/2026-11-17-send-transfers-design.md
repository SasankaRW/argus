# Argus Send: live transfers everywhere (design)

Date: 2026-11-17. Status: draft for Sas's approval (reviewed once by a builder check).

## Intent

Argus Send works (PC ↔ phone ↔ Calypso), but you can't see what it's doing. The only feedback on the PC is a
plain toast that says "Windows PowerShell". Sas wants to see a transfer happen, with progress, wherever they are
looking: the Windows notification, Ari's island on the PC, Helios (a side pop-up, a Transfers list, the live map)
and the phone. It should feel fun and alive, use current platform features, and stay light (no heavy new app).

## Goals

- **Windows notification** under the name "Argus" with the Argus icon, never "Windows PowerShell". One
  notification per transfer, with a progress bar that updates in place, then turns into the finished one
  ("Got it from my phone", with Open and Show in folder; or "Sent to Calypso"; or "Couldn't send ...").
- **Island** (ari_popup, the Qt island at the top of the PC screen): while anything moves, it shows the sending
  device's icon on the left, the receiving device's on the right, and between them the approved animation
  **"line fills, beads run ahead"** (mockup option B). The file name or "3 files to Calypso" and "41% · 12 s left"
  sit under it. This covers every transfer between any two devices, not only the PC's own.
- **Helios side pop-up**: slides in at the bottom right of any page while something moves (on the phone, above
  the dock). The newest transfer is large: from-icon, the same fill-and-beads line, the to-icon, the name,
  `62%` and `4.1 MB/s · 3 s left` in Roboto Mono. Other transfers appear as thin bars under it. A finished
  transfer pops a green "Saved to Calypso · 3 files" chip with the rotating scalloped badge, then fades. Tapping
  the pop-up opens a **Transfers list** (live ones on top, then history from `/send/history`).
- **Live map, plugin view**: Send becomes a box. While a file moves it is green like a running plugin, reads
  "phone → PC · 62%" with a small bar, and the lines from the sending device's box and into the receiving
  device's box flow (green dashes). Idle, Send is listed in the idle panel like other plugins.
- **Phone**: Android 16+ `Notification.ProgressStyle` (the phone runs Android 17): the progress notification
  with a small tracker icon (the receiving device) that moves along the bar, shown as a live update. The result
  notification stays as today.
- Smooth, never jumpy: bars glide between reports, speed and time left are smoothed.

The approved mockups sit next to this file: `2026-11-17-send-transfers-mockups-island-toast.html` (island
option B, which Sas picked, and the Windows notification) and `2026-11-17-send-transfers-mockups-map-popup.html`
(the map and the Helios pop-up with the fill-and-beads line, as approved).

## Non-goals

- No new desktop app, tray app or Electron/WebView shell. The Send agent and the island already run.
- No change to how files travel (the protocol, encryption, holding, retries stay as they are).
- No pausing transfers. Cancel only (below).
- No custom animation inside the Windows notification: Windows draws it and allows only text, icon, buttons and
  the bar. The fun lives on the island, in Helios and on the phone.

## Anti-goals (fails even if it works)

- Any console window flashing, or any notification labelled "Windows PowerShell".
- A stream of notifications (one per chunk or per percent) instead of one that updates.
- Progress writes flooding Argus's events table or database: tens of rows a second per transfer would bloat it.
- The island or Helios showing different numbers for the same transfer.
- Heavy idle cost: nothing polls fast while nothing is moving.

## Constraints

- Helios keeps its Material 3 Expressive look (SPEC.md tokens; no gradients; motion off under reduced motion;
  sentence case). The island keeps its Sonoma look (`SURFACE #16161b`, `CARD #222228`, `ACCENT #a6c8ff`,
  `GREEN #8fe3ad`), Inter and Material Symbols Rounded from `island_fonts`.
- Destructive actions are hold-to-confirm: **cancel a transfer** is a hold on the pop-up and in the list.
- The island is Qt and follows Argus with a light poll today; it must stay light.
- The PC runs Python 3.14 from the repo's `.venv`; Calypso runs the same agent on Linux; the phone is Kotlin.
- Repo flow: branch, `dev.ps1 pr`, `dev.ps1 merge local`; the dist of Helios is committed; commits carry no
  attribution lines. A phone change means a new APK that Sas installs.
- Preview before changing a real app's UI: these mockups are that preview; anything beyond them gets shown first.

## Approaches considered

- **A (picked): every agent reports live progress to Argus; Argus fans it out.** One source of truth, so every
  screen agrees, and transfers between any two devices show everywhere. Needs a phone build.
- B: only the PC's own agent drives the PC screens; Helios shows finished history only. Rejected: no live
  progress in Helios or the map, nothing for phone ↔ Calypso.
- C: Helios polls each device's agent directly. Rejected: cross-device auth and connections for no gain over A.
- Windows notification: **Start-menu "Argus" entry with an AppUserModelID + native toast with data-bound
  progress, driven in-process from the Send agent (pywinrt)** (picked) versus a separate tray app (heavier, adds
  nothing) versus Windows App SDK notifications (needs the App SDK runtime installed; more weight than value).

## How it works (decided parts)

1. **Live progress state.** Each agent, while a transfer runs, sends Argus a small progress report about every
   500 ms (and immediately on start and finish): offer id, from, to, label, files, total bytes, bytes done,
   state (`moving`, `verifying`, `done`, `failed`, `cancelled`), smoothed speed. Both ends may report the same
   offer; Argus merges by offer id (the higher byte count wins, a finish from either end ends it).
2. **Argus keeps live transfers in memory**, not in the events table, drops a finished one after ~10 s, and
   pushes changes to Helios over the live connection it already has, throttled to a few updates a second.
   Finished transfers still land in the existing Send history as today.
3. **Smoothing**: speed is an exponentially weighted average; time left comes from it and is hidden until the
   speed settles (about 2 s); bars ease between reports (Helios: CSS/WAAPI with emphasized decelerate; island: Qt
   animation at the display's frame rate) so 2 reports a second still look continuous.
4. **Windows notification**: the agent registers the "Argus" Start-menu shortcut (AppUserModelID, Argus icon from
   the shared icon recipe) if missing, then shows a toast whose progress value, title and status are bound data;
   it updates the same toast by tag and replaces it with the result toast at the end. Open / Show in folder are
   toast buttons that the agent handles. The PowerShell helper (`send/notify.py`) goes.
5. **Island**: a "transfers" state that takes priority over idle but not over Ari listening/speaking; it polls
   Argus's live transfers only while something is moving (and checks rarely when idle, or wakes on the event
   stream it already reads).
6. **Phone**: `SendNotes` uses `Notification.ProgressStyle` on Android 16+ (tracker icon = receiving device,
   segments per file when there are several), the old progress notification below that; `SendEngine` and
   `Receiver` send the same progress reports to Argus.
7. **Map**: Send is a node of kind plugin in the plugin view, connected to the device boxes; its status text and
   bar come from the live transfers; its lines use the existing `busy` styling.

## Calls made after review (Claude's, for Sas to check)

- **Cancel works from anywhere, for any transfer** (they're all Sas's devices). A hold on the Helios pop-up, the
  Transfers list or the island asks Argus to cancel; Argus answers the sending agent's next progress report
  (every ~500 ms while moving) with "cancel", and the sender stops as it does today with its local cancel. The
  receiver is never asked to refuse chunks, so the protocol is unchanged. The PC toast and the phone notification
  keep their own Cancel (local, instant). Cancelling a share Calypso is holding drops the held copy.
- **A share through Calypso's holding area shows as one transfer**, phone → PC "via Calypso", with two stages:
  "to Calypso" (first leg) and "to PC" (second leg, which may start much later). Argus keys live transfers by
  offer id plus leg, so the first leg finishing doesn't end the second. The map draws phone → Send → Calypso for
  the first leg and Calypso → Send → PC for the second. The final "Sent to PC" fires only when the second leg ends;
  after the first, the sender sees "Waiting on Calypso for PC".
- **Only moving transfers show on the island, the pop-up and the map.** Queued, held and waiting-to-retry items
  appear in the Transfers list (with their reason, e.g. "PC is off; Calypso is holding it") and as a small
  "1 waiting" note on the map's Send box. Nothing reports on a timer while nothing moves: an agent reports a
  waiting item once when its state changes. So the island never sits on screen for a share held for days.
- Live progress travels on a new side channel for messages that aren't saved, separate from the events-table
  stream (`/ws/events`), which stays as it is.

## Left to the builder

- Exact endpoint names and payload field names; whether Helios gets progress via the existing WebSocket topic or
  a new one; the in-memory store's shape.
- Which pywinrt modules and how the toast button activation reaches the agent (protocol activation or COM),
  as long as nothing shows a console and the toast says "Argus".
- The island's exact layout within the approved mockup, its poll interval while busy, and how it slots into
  its existing state priority.
- Tests: unit tests for merging, smoothing and the throttle; Helios component logic tests; the existing Send
  suites must keep passing.
- The order of delivery. Suggested: (1) agent progress + Argus live state + Windows notification; (2) Helios
  pop-up, Transfers list and map; (3) island; (4) phone. Each piece is usable on its own.
