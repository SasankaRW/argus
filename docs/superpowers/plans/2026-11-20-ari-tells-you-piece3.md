# Ari tells you, piece 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (chosen: native). Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stock and price news reaches you through Ari: said straight away when you're at the PC, kept and said in one short line when you're back, and written into your Ari chat.

**Architecture:** Argus already raises an `ari.notice` event for every phone message of priority ≥ 4 (`outbox.add_message`), and the PC listener (`ari_listen.watch`) speaks it only if `ari.speak_up` is on (default off) and you're at the PC right then; otherwise it is dropped. A small pure `NoticeKeeper` (new `core/argus/ari/notices.py`) decides now / keep / catch-up; the listener feeds it and asks it on every loop. Wishlist messages also become an Ari turn in an "updates" chat.

**Tech Stack:** Python 3.12 (core, the PC listener), React/TS (Helios banner text).

**Spec:** `docs/superpowers/specs/2026-11-20-live-map-and-stock-alerts-design.md`, part 3 and settled items 4 and 6.

## Global Constraints

- At the PC = `idle_seconds() < 300`, not paused, inside `ari.speak_hours`, not in a conversation, nothing playing.
- Gap between spoken notices: 600 s (as today), and it now delays rather than drops.
- Kept: at most 5, each dropped 24 h after it arrived. Back = at the PC again after being away (any of: idle ≥ 300 s before, paused before, outside speak hours before).
- Catch-up line: one kept → `While you were away: <notice>.`; two or three → `While you were away: <a>, and <b>.` (names up to three); more → the three newest and `and N more`.
- `ari.speak_up` default becomes true.
- Only messages tagged `wishlist` go into the chat, conv id `updates`, shown as "Updates from Argus".
- Commits `type(scope): msg`, summary in lower case, first line ≤ 72.

## Review Focus

1. A burst of notices while away (5 restocks overnight) → one catch-up line, not five: test in Task 1.
2. Back after a day → old (> 24 h) notices are not said: test in Task 1.
3. A notice while at the PC but within 600 s of the last → kept, said when the gap passes, not dropped: test in Task 1.
4. The same message twice (a retried job) → said once: dedupe by title+text in the keeper, test in Task 1.

---

### Task 1: `NoticeKeeper`

**Files:** Create `core/argus/ari/notices.py`; Test `tests/test_ari_notices.py`

**Interfaces:**
- `class NoticeKeeper: add(title: str, text: str, now: float) -> None; take(now: float, here: bool) -> str | None`
  - `here`: at the PC and free to speak right now (the listener works out idle / paused / hours / busy).
  - Returns the sentence to say, or None. A notice that arrives while `here` and no gap is pending comes back as itself (`notice_text`, as today); kept ones come back as the catch-up line the first time `take(..., here=True)` is called after a call with `here=False` or after the gap.
  - `MAX_KEPT = 5`, `KEEP_S = 86400`, `GAP_S = 600`.
- `def catch_up_line(items: list[str]) -> str` (wording above).

- [ ] Step 1: tests: `test_said_at_once_when_here`, `test_kept_while_away_and_said_once_back` (five adds while away → one line naming three + "and 2 more"), `test_old_news_is_not_said` (added, then take 86401 s later → None), `test_the_gap_delays_not_drops`, `test_the_same_notice_twice_is_said_once`, `test_catch_up_wording` (1, 2, 3, 5 items).
- [ ] Step 2: `.venv\Scripts\python -m pytest tests/test_ari_notices.py -q` → FAIL (module missing)
- [ ] Step 3: implement
- [ ] Step 4: same → PASS
- [ ] Step 5: commit `feat(ari): keep notices while you're away and say them when you're back`

### Task 2: the listener uses it; speak up by default

**Files:** Modify `core/argus/ari/ari_listen.py` (`watch()`), `core/argus/config.py` (`speak_up: bool = True`); Test: `tests/test_ari_listen.py` (existing notice tests)

- Every `ari.notice` event goes to `keeper.add(title, text, time.time())` (instead of being said or dropped).
- Each loop of `watch()` (it wakes at least every 20 s): `here = cfg.ari.speak_up and not paused() and speak_hours_ok(...) and idle_seconds() < 300 and not player.busy and not talk.in_conversation`; `line = keeper.take(time.time(), here)`; if line: log "speaking up" and `player.say(vl.sentences(line))`.
- [ ] Step 1: update/extend the existing listener notice test: a notice while idle 400 s isn't said; after idle drops to 10 s the catch-up line is said (fake clock/idle as the existing tests do).
- [ ] Step 2: run → FAIL
- [ ] Step 3: implement
- [ ] Step 4: `tests/test_ari_listen.py` and `tests/test_ari_notices.py` → PASS
- [ ] Step 5: commit `feat(ari): say stock news now, or when you're back at the PC`

### Task 3: wishlist news in the Ari chat

**Files:** Modify `core/argus/outbox.py` (`add_message`, next to the `ari.notice` insert), `core/argus/ari/ari.py` (`chats`: title fallback); Test: `tests/test_watchers.py` or a new `tests/test_ari_updates_chat.py`

- When a phone message's tags include `wishlist`: `add_turn(conn, "updates", "ari", f"{title without 'Watcher: '/'Wishlist: '}: {message}")` in the same transaction.
- `chats()`: a conv with no "you" turn takes the title "Updates from Argus" when its id is `updates`.
- [ ] Step 1: test: queue a wishlist message → `chats()` lists `updates` titled "Updates from Argus" with the turn text `Dune book: Wishlist: Dune book is back in stock at LKR 4,495.5.` cleaned to `Dune book is back in stock at LKR 4,495.5.`; a non-wishlist message adds no turn.
- [ ] Step 2: run → FAIL; Step 3: implement; Step 4: run + `tests/test_ari*.py` → PASS
- [ ] Step 5: commit `feat(ari): wishlist news goes into an updates chat`

### Task 4: Helios says it, and a switch

**Files:** Modify `helios/src/MapNews.tsx` (banner line: `Told your phone and Ari` for wishlist news, per the piece 1 ruling); Helios Settings: add the "Ari says important news out loud" switch only if the settings page already edits `ari.*` config keys at runtime (check `core/argus/settings.py`); otherwise ledger a ruling and leave the switch for later (default on covers the need).
- [ ] tests: `npx vitest run`, `npm run typecheck`; rebuild `core/argus/helios_dist`; commit `feat(helios): the news banner says Ari was told too`

### Task 5: ship

- [ ] CHANGELOG ("Ari: important news is said straight away at the PC, or in one line when you're back; wishlist news also goes into an 'Updates from Argus' chat; speak up is on by default"); full `pytest`; final review; `dev.ps1 pr` + `merge`.
