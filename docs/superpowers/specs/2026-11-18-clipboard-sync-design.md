# Clipboard and link sync (design)

Approved by Sas on 2026-11-18. It's the first of three features; live activities on the island and the morning briefing follow as separate designs.

## Intent
- Copy on the PC and paste on the phone without thinking about it.
- Copy on the phone and get it on the PC with one tap.
- A link shared from one device arrives on the other as a notification that opens it.
- It should be cheap: event-driven, with no polling, nothing stored, and the existing Send pairing, keys and transport reused.

## Goals
- **PC to phone is automatic** (Sas chose this over on-demand). Every text copy on the PC lands on the phone's clipboard within about 2 s while both are online.
- **Phone to PC takes one tap.** Android 10 and later blocks background clipboard reads, so it works through:
  - a quick-settings tile, "Send clipboard";
  - copies made inside the Argus app, which are sent automatically;
  - the share sheet with text ("Argus → PC").
- **Shared links become notifications** with Open and Copy (Sas's choice). On the PC this is a native Argus toast; on the phone, an Android notification. A link that was copied, not shared, is plain text and goes to the clipboard.
- **An on/off switch on each device:** the island menu on the PC and the Send screen on the phone.

## Non-goals (for now)
- Images, files or rich formats on the clipboard. Files already go through Send.
- More than one phone or PC getting the same clip. The target is the peer device set in the switch; the default is the single paired phone or PC.
- Clipboard history.

## Anti-goals
These would make it a failure even if it works:
- Passwords leaving the PC.
- A CPU timer that polls the clipboard.
- Clipboard text written to disk or logs on any device, including Calypso.
- Echo loops between devices.

## Decided design

### PC watcher (in the PC Send agent, Windows only)
- A hidden message-only window (`HWND_MESSAGE`) on its own thread, registered with `AddClipboardFormatListener`, wakes on `WM_CLIPBOARDUPDATE`. It uses ctypes and needs no new dependency. Nothing runs between copies.
- On an update it debounces 300 ms, so only the last of a burst of copies is sent. Then it opens the clipboard and checks the exclusion formats. It skips the copy if `ExcludeClipboardContentFromMonitorProcessing` is present, or `CanIncludeInClipboardHistory` or `CanUploadToCloudClipboard` holds a DWORD 0.
- Only then does it read `CF_UNICODETEXT`. Empty text, text over 256 KB, and non-text copies are skipped.
- **Echo guard.** It keeps the SHA-256 of the last text it wrote to the clipboard itself, from an incoming clip. If a copy matches that hash, it isn't sent back.
- **The same text twice in a row is sent once,** compared by hash.
- **Errors.** A failure to open the clipboard because another app holds it gets 3 retries 50 ms apart, then the copy is dropped.

### Wire format
- A new route `POST /send/clip`, on both the Python receiver and the Kotlin receiver. It uses the existing session: hello, then `X-Send-*` headers and `request_mac`.
- **The body is encrypted, unlike file chunks.** This is AES-256-GCM. The key is an HKDF-SHA256 of the session key with info `"argus clip v1"`, and the nonce is a random 12 bytes. The body is JSON: `{"n": b64 nonce, "c": b64 ciphertext}`.
- The plaintext is JSON: `{"id": uuid, "kind": "copy"|"link", "text": str, "at": unix_ms, "from": device_id, "for": device_id}`.
- **The receiver keeps the last 64 ids** and ignores repeats.
- **The receiver ignores a clip older than its newest applied `at`** from the same sender. So a clip that arrives late after a fresher one is dropped.

### Delivery
- **Direct first.** The sender reuses or opens a link to the target, using the same `probe` as Send (LAN first, then Tailscale) with a 3 s budget.
- **If the target is unreachable,** it posts the clip to Calypso with `for` set to the target. Calypso keeps only the latest clip per target, in memory only, for 10 minutes.
- **Calypso pushes it** the next time the target says hello to Calypso, or when Calypso's discovery or Tailscale loop sees the target come online. After it's delivered or expires, the clip is discarded.
- **Never queued to disk** on any device. If the agent restarts, pending clips are lost, by design.
- A new clip for the same target replaces the pending one.

### Receiving
- **PC:**
  - A `copy` clip sets `CF_UNICODETEXT`. It records the hash for the echo guard and sets `ExcludeClipboardContentFromMonitorProcessing`, so Windows clipboard history doesn't keep phone copies twice.
  - A `link` clip shows the existing Argus toast (`send/toast.py`) with Open (default browser via `os.startfile`) and Copy.
  - Text is not shown in the toast beyond the link's host and path, shortened to 60 characters.
- **Phone:**
  - A `copy` clip calls `ClipboardManager.setPrimaryClip` from the Send service. On API 33 and later the ClipDescription carries `EXTRA_IS_SENSITIVE=true`, so the system preview doesn't show the text. No notification.
  - A `link` clip posts a notification on a new channel, "Links", with Open (ACTION_VIEW) and Copy actions. Tapping the notification opens the link.

### Phone sending
- **Quick-settings tile "Send clipboard".**
  - Tapping it starts a transparent, no-animation activity (`excludeFromRecents`, `noHistory`).
  - The activity reads the clipboard in `onWindowFocusChanged(true)`, which is the only moment Android allows it, sends a `copy` clip through SendEngine's sender, shows a short toast ("Sent to PC"), and finishes.
  - The tile shows inactive when sync is off.
- **Copies inside the Argus app**, in the WebView or native screens. An `OnPrimaryClipChangedListener` registered only while MainActivity is in the foreground sends them.
- **Share sheet.** `SendShareActivity` gains `text/plain` with `EXTRA_TEXT`. If the text is a single http(s) URL it's sent as `link`, otherwise as `copy`. Shared files keep today's path.

### PC sending links
- The island menu gets a "Send link to phone" action for the current clipboard when it holds a URL.
- `argus send --link <url>` is added for scripts.
- Both send a `link` clip.

### Switch and settings
- `send.clip.enabled` (default true) and `send.clip.peer` (a device id; the default is the only paired device of the other kind) go in argus.yaml for the PC.
- The phone has a switch in the Send screen, stored in Prefs. The island menu toggles the PC value at runtime through a local agent route `POST /send/clip/enabled`, which needs the worker token, and persists it to a small `data/clip.json`.
- When sync is off, no clip is read or sent, and incoming clips are refused with 409.

### Privacy
- Clip text is never logged, never written to disk and never sent to Argus core or Helios.
- Logs record only `clip sent to <device> (<n> chars)`.
- Calypso holds pending clips in RAM only.

### Resource budget
- PC idle cost: one blocked thread waiting on GetMessage.
- Phone idle cost: nothing new. The receiver already listens, and the tile and activity run only on tap.

## Left to the builder
- How the Calypso push hooks into existing loops.
- Class and file names: suggested `send/clip.py` with `clipwin.py` for the Win32 part, and `send/Clip.kt` plus `ClipTile.kt` with `ClipActivity.kt`.
- Test layout. The Win32 watcher is behind a small interface so the debounce, exclusion, echo and dedupe logic is unit-tested without Windows. The AES-GCM wire format gets tests on both sides with a shared test vector.
- How the island exposes the switch, following the existing menu style. A preview is needed before merge (house rule).

## Shipping
- One PR for core and island, and one PR for the phone app, via `dev.ps1 pr` then `merge local`.
- The phone app goes to version 1.6.
- Commits use the form `type(scope): msg`.

# Decisions after the builder check (2026-11-18) - these replace the sections above where they differ

1. **The relay re-encrypts.** Calypso is the user's own server and already holds shared files in plaintext. It decrypts a relayed clip in RAM with its own session key and re-encrypts it with the target's session key when it pushes. The plaintext `from` keeps the origin device. The receiver trusts it only when the session peer is the holder (`send.holding` device, i.e. calypso), the same rule held offers use for `origin`. Dedupe by `id` and the `at` check apply to the origin's `from`.
2. **Peers.** Clip endpoints are the PC and the phone only; Calypso is never an endpoint, only the relay. `send.clip.peer` defaults to `phone` on the PC, and the phone app's default peer is `pc`. Both are configurable; device ids are the existing conventional ones.
3. **The relay push uses existing loops.** Calypso's pending map lives in RAM, `{target: clip}`, with a 10 min TTL. On every `wake()`, and on a 30 s timer that runs only while something is pending, Calypso probes each pending target and pushes. Delivery up to about 30 s after an offline device reconnects is accepted; the 2 s goal applies when both are online.
4. **The phone falls back in memory.** The phone's clip send is its own small path: probe the peer (3 s), else hello Calypso and `POST /send/clip` with `for`. It never touches SendQueue, the outbox or the DB.
5. **The island calls the local agent directly** at `http://127.0.0.1:8610`, with the worker token it already loads (`ARGUS_WORKER_TOKEN`, same as the agent's `argus_token`), for `POST /send/clip/enabled`, `GET /send/clip` (state) and `POST /send/clip/link`.
6. **Ordering per sender only.** `at` is compared only with earlier clips from the same `from`, so different device clocks never meet. A clip whose `at` is more than 5 minutes older than the newest one applied from that sender is dropped, which tolerates small clock steps.

## Shared test vector (both sides must pass)
- Session key: bytes 0x00..0x1f.
- HKDF-SHA256 with salt none (RFC 5869 default: 32 zero bytes), info `argus clip v1`, length 32. The resulting clip key is `b52c0827574da950beaf28d43afd56c5d15acd46885808c023a619251c9bf3a4`.
- AES-256-GCM with nonce bytes 0x00..0x0b, no AAD, plaintext `{"text":"hello"}`. The ciphertext plus tag, base64-encoded, is `OOYJvIsdC7IC6jK/7SwwxP9ypPKsANs3XdeBa0E92G4=`.
