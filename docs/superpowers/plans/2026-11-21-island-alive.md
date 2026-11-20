# A livelier island Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (chosen: native). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Ari's island (PySide6, `core/argus/ari/ari_popup.py`) gets what Sas approved in the preview: smooth page flips, a dot that stretches into a pill, rows rising in, a compact start page, small live details, ripples, and a now-playing strip only while something plays.

**Design:** the approved preview, `docs/superpowers/specs/2026-11-21-island-alive-preview.html` (https://claude.ai/artifact/GPyxKMZnTDosgULhUn3ACE, version 4). The Sonoma look stays (Inter, Material Symbols Rounded, the five tones on black); no gradients, no hover lift. Google Calendar holidays and events are a separate piece (a calendar plugin; it needs Sas to switch on the Calendar API) — the calendar page only gets its motion here.

**Architecture:** the island is painted by hand each frame. The timing maths goes in a new pure module `core/argus/ari/island_motion.py` (tested); `ari_popup.py` keeps a few timestamps (flip start and direction, page entered, ripples) and asks that module where to draw things. The existing `pulse` timer already repaints every frame while the island is open; nothing moves while it is folded, and `reduced_motion()` (Windows "animation effects" off) makes every transition finish at once.

**Tech:** Python 3.12, PySide6 (`pip install -e .[popup]`), pytest.

## Global Constraints

- Flip: 460 ms, emphasized decelerate; old page moves 36 px away, fades, scales to .96; new page comes from 36 px on the other side.
- Rows rise 8 px and fade in, 460 ms, 30 ms apart, when the island opens or a page arrives.
- Current dot 14 px wide (the old 14 stays), others 5; width eases with the flip.
- Start page (top to bottom): clock (colon blinks every second) + date + today's weather; game, power, Ari's orb (5-bar waveform that idles, rings that breathe); three tiles (Argus status, waiting, next job with a ring closing over its last hour and "N min"); the now-playing strip only while media plays; clipboard switch + Send link; shortcuts as a row of icon + label (4 per row); the morning brief row and "Ari's last answer" stay where they are now when on.
- Media: moving equalizer bars while playing. System: bars ease to new values; a CPU line from the last 40 samples. Calendar: today's circle pulses.
- Ripple: from the press point, clipped to the key, 600 ms.
- Copy: sentence case, no full stops on labels.

## Review Focus

1. Clicking during a flip: buttons must be the new page's only (the old page is drawn but its keys are not added). Test via `island_motion` + a check in Task 3 that `self.buttons` is rebuilt from the new page only.
2. Pages appearing or vanishing mid-flip (media stops while flipping to it): the page index is clamped and the flip ends cleanly — `flip_frame` with t ≥ 1 returns the end state.
3. Reduced motion: every function returns its end state when `still=True`; test it.
4. Long labels in tiles and shortcuts are elided, never drawn past the tile: use `QFontMetrics.elidedText`.

---

### Task 1: `island_motion.py`

**Files:** Create `core/argus/ari/island_motion.py`; Test `tests/test_island_motion.py`

**Interfaces (all pure):**
- `ease(t: float) -> float` — emphasized decelerate (cubic-bezier .05,.7,.1,1), clamped 0..1.
- `flip_frame(t: float, direction: int, still: bool = False) -> tuple[Pose, Pose]` with `Pose = (dx, alpha, scale)` for the old and new page; t = elapsed / 0.46 s.
- `rise(elapsed: float, index: int, still: bool = False) -> tuple[float, float]` — (dy, alpha).
- `dot_widths(n: int, page: int, prev: int, t: float, still: bool = False) -> list[float]`.
- `countdown(now: float, due: float, window: float = 3600) -> tuple[float, str]` — (fraction of the ring still open 0..1, "23 min" / "2 h" / "now").
- `wave(t: float, n: int = 5) -> list[float]` — bar heights 0.25..0.85, each phase-shifted.
- `approach(cur: float, target: float, dt: float, half: float = 0.15) -> float` — eases a bar toward its value.
- `ripple(elapsed: float) -> tuple[float, float] | None` — (scale 0..1, alpha), None when done (600 ms).

- [ ] Step 1: tests for each (end states, monotonic ease, stagger order, still=True gives end state, countdown wording: 23 min, 2 h, now; ripple None after 0.6 s).
- [ ] Step 2: run → FAIL; Step 3: implement; Step 4: run → PASS
- [ ] Step 5: commit `feat(island): the timing for a livelier island`

### Task 2: smooth flips, the stretching dot, rows rising in

**Files:** Modify `core/argus/ari/ari_popup.py` (`flip`, `open_details`, `draw_details`, `draw_dots`, page draw functions get a `rise` index per block)

- `flip()` keeps `flip_from`, `flip_dir`, `flip_t0`; `draw_details` draws the old page (keys discarded) then the new one with `flip_frame`; the height follows the new page as today.
- [ ] Step 1: extend `tests/test_ari_popup.py` (it renders with `--preview`): rendering `--details --page system` still works and a new `flip_at`-style test (set `flip_from`, `flip_t0` and render mid-flip) produces no exception and only the new page's keys in `buttons`.
- [ ] Step 2–4: fail, implement, pass; render the four pages to PNGs and look at them.
- [ ] Step 5: commit `feat(island): pages glide when you flip, rows rise in`

### Task 3: the compact start page

**Files:** Modify `draw_ari` (and small helpers: `orb`, `tile3`, `switch_row`, `shortcut_grid`, `now_playing`)
- [ ] Step 1: test: `--preview --details --page ari` renders; with `media` playing the strip is in `buttons` as `page:media`, without it isn't.
- [ ] Step 2–4; render and compare with the preview.
- [ ] Step 5: commit `feat(island): a compact start page with tiles and Ari's orb`

### Task 4: live pages and ripples

**Files:** Modify `draw_media` (equalizer), `draw_system` (eased bars, CPU line), `draw_calendar` (today pulses), `mousePressEvent` + `paintEvent` (ripples).
- [ ] Step 1: tests for the eased values via `approach` (Task 1) and that a press adds a ripple that is gone after 0.6 s (`ripples` list).
- [ ] Step 2–4; render.
- [ ] Step 5: commit `feat(island): moving music bars, easing load bars and ripples`

### Task 5: ship

- [ ] Copy the preview HTML into `docs/superpowers/specs/2026-11-21-island-alive-preview.html`; CHANGELOG; full `pytest`; review; `dev.ps1 pr` + `merge`. The PC supervisor restarts the island on new code.
