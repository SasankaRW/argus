# Send live transfers, piece 1: progress reports and the Argus notification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** While a file moves, every Send agent reports live progress to Argus, Argus keeps the live transfers in memory (with cancel), and the PC shows one native Windows notification as "Argus" whose progress bar fills in place and then becomes the result.

**Architecture:** A pure `Progress` tracker (smoothed speed, time left, throttle) lives in the agent per transfer leg. The sender and the receiver feed it; a report loop posts snapshots to Argus `POST /send/live` about every 500 ms while anything moves, and Argus's reply carries cancel requests. Argus holds live transfers in memory only (never the events table). On Windows the agent drives a native toast through pywinrt with data-bound progress; the "Argus" name and icon come from a per-user AppUserModelID registry key, no Start-menu app and no PowerShell.

**Tech Stack:** Python 3.14, FastAPI, httpx, asyncio, pywinrt 4.x (`winrt-Windows.UI.Notifications`, `winrt-Windows.Data.Xml.Dom`), winreg, pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-send-transfers-design.md` (approved by Sas 2026-11-17). Pieces 2-4 (Helios pop-up, list and map; island; phone) are separate plans.

## Global Constraints

- Never a console window, and never a notification labelled "Windows PowerShell"; `send/notify.py` (the PowerShell helper) is removed.
- One notification per transfer that updates in place (same tag), never one per chunk or per percent.
- Progress never goes into Argus's events table or database; finished transfers still land in Send history as today.
- Nothing polls while nothing moves: the report loop only runs while a transfer is live.
- The Send protocol (offers, chunks, MAC) does not change; cancel stops the sender only.
- Speed is an exponentially weighted average; time left is hidden until the speed has settled (about 2 s).
- Argus keys live transfers by offer id plus leg (`send` or `hold`), the higher byte count wins, a finish from either end ends that leg, and finished legs drop after ~10 s.
- Cancel works from anywhere for any transfer; cancelling a held share drops Calypso's copy.
- Sentence case for every user-facing string. Commits carry no attribution lines (the repo's hook rejects them).

## Review Focus

- Argus restarts mid-transfer: the agent keeps sending; the next report recreates the live entry (no error, no duplicate).
- A transfer that fails or is cancelled: the toast turns into "Couldn't send …" / "Cancelled", never stays at a frozen bar.
- Tiny files (done before the first report): exactly one result toast, no progress toast flash.
- pywinrt missing or the toast call raising (Windows N editions, Focus assist): the transfer is unaffected; it is logged once.
- Two transfers at once: two separate toasts (different tags) and two live entries.

---

### Task 1: The progress tracker

**Files:**
- Create: `core/argus/send/progress.py`
- Test: `tests/test_send_progress.py`

**Interfaces:**
- Produces: `class Progress` with `__init__(self, offer_id: str, leg: str, src: str, dst: str, label: str, files: int, total: int, now: float)`, `update(self, done: int, now: float, state: str = "moving") -> None`, `due(self, now: float, every: float = 0.5) -> bool` (True at most every `every` s, always True right after a state change), `snapshot(self) -> dict` with keys `offer_id, leg, from, to, label, files, total, done, state, speed, eta` (`speed` bytes/s rounded, `eta` seconds or `None`). States: `moving`, `verifying`, `done`, `failed`, `cancelled`.

- [ ] **Step 1: Write the failing tests**

```python
def test_speed_is_smoothed_and_eta_waits_until_it_settles():
    p = Progress("o1", "send", "pc", "phone", "a.bin", 1, 10_000_000, now=0.0)
    p.update(1_000_000, now=0.5)
    assert p.snapshot()["eta"] is None            # under 2 s: not settled yet
    for i in range(2, 9):
        p.update(1_000_000 * i, now=0.5 * i)
    s = p.snapshot()
    assert 1_800_000 < s["speed"] < 2_200_000      # 1 MB every 0.5 s
    assert 2.5 < s["eta"] < 3.5                    # 6 MB left at ~2 MB/s

def test_due_throttles_but_state_changes_report_at_once():
    p = Progress("o1", "send", "pc", "phone", "a", 1, 100, now=0.0)
    assert p.due(0.0) and not p.due(0.2) and p.due(0.6)
    p.update(100, now=0.7, state="done")
    assert p.due(0.71)
```

- [ ] **Step 2: Run them, expect ImportError**

Run: `.venv/Scripts/python -m pytest -q -p no:cacheprovider --basetemp $TEMP/pt tests/test_send_progress.py`

- [ ] **Step 3: Implement `Progress`** — EWMA with alpha 0.3 over instantaneous rate between updates; `eta = (total - done) / speed` once `now - started >= 2` and speed > 0, else `None`; a state change resets the throttle.

- [ ] **Step 4: Run, expect PASS; commit** `feat(send): a progress tracker with smoothed speed and time left`

### Task 2: Argus keeps live transfers (and cancel)

**Files:**
- Create: `core/argus/send_live.py` (the in-memory store; no FastAPI)
- Modify: `core/argus/api/routes/send.py` (three routes), `core/argus/context.py` (one `SendLive()` on `Argus`)
- Test: `tests/test_send_live.py`

**Interfaces:**
- Consumes: the `snapshot()` dict shape from Task 1.
- Produces: `class SendLive` with `report(self, device: str, items: list[dict], now: float) -> list[str]` (merges, returns offer ids the device must cancel: requests for items it reported as the sender `from == device`), `items(self, now: float) -> list[dict]` (live first, newest first; drops finished legs older than `KEEP_DONE = 10.0` s; each item gets `device`, `updated`), `cancel(self, offer_id: str) -> bool` (False if unknown). Routes (worker-token guarded like the others): `POST /send/live` body `{"device": str, "items": [snapshot...]}` → `{"cancel": [offer_id...]}`; `GET /send/live` → `items()`; `POST /send/live/{offer_id}/cancel` → `{"ok": bool}`. The routes' docstrings say "kept in memory only".

- [ ] **Step 1: Write the failing tests** (`SendLive` directly, then the routes through `TestClient` with `make()` from `tests/test_send_api.py`)

```python
def test_both_ends_merge_and_a_finish_ends_the_leg():
    lv = SendLive()
    snap = {"offer_id": "o" * 8, "leg": "send", "from": "phone", "to": "pc", "label": "a", "files": 1,
            "total": 100, "done": 40, "state": "moving", "speed": 10, "eta": 6}
    lv.report("phone", [snap], now=0.0)
    lv.report("pc", [{**snap, "done": 55}], now=0.1)          # the receiver is ahead: the higher count wins
    assert [i["done"] for i in lv.items(0.2)] == [55]
    lv.report("phone", [{**snap, "done": 50}], now=0.3)       # never goes backwards
    assert lv.items(0.3)[0]["done"] == 55
    lv.report("pc", [{**snap, "done": 100, "state": "done"}], now=0.4)
    assert lv.items(5.0)[0]["state"] == "done" and lv.items(11.0) == []

def test_held_share_legs_are_separate():
    lv = SendLive()
    base = {"offer_id": "h" * 8, "from": "phone", "to": "calypso", "label": "a", "files": 1, "total": 9,
            "done": 9, "state": "done", "speed": 0, "eta": None}
    lv.report("phone", [{**base, "leg": "send"}], now=0.0)
    lv.report("calypso", [{**base, "leg": "hold", "from": "calypso", "to": "pc", "done": 1, "state": "moving"}], now=0.1)
    assert {(i["leg"], i["state"]) for i in lv.items(0.2)} == {("send", "done"), ("hold", "moving")}

def test_cancel_reaches_the_sender_on_its_next_report():
    lv = SendLive()
    snap = {"offer_id": "c" * 8, "leg": "send", "from": "pc", "to": "phone", "label": "a", "files": 1,
            "total": 9, "done": 1, "state": "moving", "speed": 1, "eta": None}
    lv.report("pc", [snap], now=0.0)
    assert lv.cancel("c" * 8) and not lv.cancel("x" * 8)
    assert lv.report("phone", [], now=0.1) == []              # not the sender
    assert lv.report("pc", [snap], now=0.2) == ["c" * 8]
    assert lv.report("pc", [snap], now=0.3) == []             # asked once
```

Route test: unauthenticated `POST /send/live` → 401; authenticated post then `GET /send/live` returns the item with `device`; `POST /send/live/{id}/cancel` → `{"ok": True}` then the sender's next post returns it in `cancel`.

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/test_send_live.py`
- [ ] **Step 3: Implement** `SendLive` (dict keyed `(offer_id, leg)`; pending cancels a set of offer ids, popped when returned) and the three routes.
- [ ] **Step 4: Run, expect PASS; commit** `feat(send): Argus keeps live transfers in memory, with cancel`

### Task 3: The agent reports progress and can be cancelled

**Files:**
- Modify: `core/argus/send/agent.py` (live trackers, report loop, `cancel`), `core/argus/send/sender.py` (feed progress; stop on cancel), `core/argus/send/receiver.py` (feed incoming progress), `core/argus/send/cli.py` (`argus send cancel <offer_id>`), `core/argus/send/__main__.py` if needed for `pythonw -m argus.send`
- Test: `tests/test_send_live_agent.py`

**Interfaces:**
- Consumes: `Progress` (Task 1); `POST /send/live` reply shape (Task 2).
- Produces on `SendAgent`: `self.live: dict[tuple[str, str], Progress]`; `progress(self, offer_id: str, leg: str, *, src: str, dst: str, label: str, files: int, total: int, done: int, state: str = "moving") -> None` (creates or updates, wakes the report loop, calls `self.on_progress(p)` — a hook Task 4 sets; default no-op); `cancel(self, offer_id: str) -> bool` (marks the queue item `cancelled` with reason "cancelled", records history event `"cancelled"`, drops a holding copy if it is a hold; True if found). `Cancelled(Exception)` in `sender.py`: `_chunk` checks `store.get(oid)["state"] == "cancelled"` at the existing 0.5 s save point and raises it; `process` catches it, leaves the state `cancelled`, reports `state="cancelled"`. The report loop (`_live_loop`) runs only while `self.live` has a non-finished entry (event-driven, waits on a wake event otherwise), posts every 0.5 s, posts once more after finish, then forgets finished entries; it never raises (Argus down → keep going silently). CLI: `argus send cancel <offer_id>` calls `agent.cancel` (queue store only; no agent needed).

- [ ] **Step 1: Write the failing tests** using `tests/_send_kit.py` (`make_agent`, `pair`, `make_file`, `wait_state`) and a fake Argus like `test_argus_sync_with_a_fake_argus`:
  - `test_a_transfer_reports_progress_then_done`: a 12 MB send pc→phone with `chunk_bytes` small (as other transfer tests do); the fake `/send/live` collects bodies; assert at least one `moving` snapshot with `0 < done < total`, the last snapshot for the leg is `done` with `done == total`, and no posts arrive after the loop has gone quiet for 2 s.
  - `test_cancel_from_argus_stops_the_sender`: fake replies `{"cancel": [oid]}` once `done > 0`; `wait_state(a, oid, "cancelled")`; the receiver has no finished file; the history has a `cancelled` event.
  - `test_cli_cancel_marks_the_item`: `cli.main(["--config", cfg, "send", "phone", file])` then `cli.main([... "send", "cancel", oid])` → item state `cancelled`.
  - `test_argus_down_doesnt_stop_a_send`: `argus_url` pointing at a closed port; the transfer still reaches `done`.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** as specified in Interfaces. Incoming side: the receiver calls `agent.progress(offer_id, "hold" if hold else "send", src=origin-or-from, dst=self id, ...)` with the bytes held so far, and `state="done"` from `finish`.
- [ ] **Step 4: Run all Send tests, expect PASS** — `pytest tests/test_send_*.py`
- [ ] **Step 5: Commit** `feat(send): agents report live progress to Argus and can be cancelled`

### Task 4: The "Argus" notification with a live progress bar

**Files:**
- Create: `core/argus/send/toast.py`
- Delete: `core/argus/send/notify.py`; update `core/argus/send/cli.py` and `core/argus/send/agent.py` to use `toast.py`
- Modify: `pyproject.toml` (`send` extra: `"winrt-runtime>=3; sys_platform == 'win32'"`, `"winrt-Windows.UI.Notifications>=3; sys_platform == 'win32'"`, `"winrt-Windows.Data.Xml.Dom>=3; sys_platform == 'win32'"`, `"winrt-Windows.Foundation>=3; sys_platform == 'win32'"`, `"winrt-Windows.Foundation.Collections>=3; sys_platform == 'win32'"`), `tests/test_send_menu.py` (the notify tests move to the new module)
- Test: `tests/test_send_toast.py`

**Interfaces:**
- Consumes: `SendAgent.on_progress(p: Progress)` hook and history events (Task 3).
- Produces: `APP_ID = "Argus.Send"`; `register_app(icon: Path) -> bool` (writes `HKCU\Software\Classes\AppUserModelId\Argus.Send` with `DisplayName = "Argus"`, `IconUri = <icon>`; idempotent; False off Windows); `register_cancel_protocol(pythonw: Path) -> bool` (`HKCU\Software\Classes\argus-send` URL protocol whose open command is `"<pythonw>" -m argus.send cancel-url "%1"` — pythonw, so no console); pure builders `progress_xml(title: str, label: str) -> str` and `result_xml(title: str, body: str, buttons: list[tuple[str, str]]) -> str` (buttons are `(content, launch_uri)` with `activationType="protocol"`), `progress_values(p: Progress) -> dict[str, str]` (`progressValue` "0.00"-"1.00", `progressValueString` like "62%", `progressStatus` like "4.1 MB/s · 3 s left" or "4.1 MB/s"); `class Toaster` with `show(p: Progress) -> None` (first call per offer shows the progress toast with tag = offer id, group "send"; later calls `ToastNotifier.Update(NotificationData, tag, group)`; results replace the toast under the same tag) and `problem(title: str, body: str) -> None`; every Toaster method swallows and logs (once) any error. Titles (sentence case): moving out "Sending to my phone", in "Receiving from my phone"; done out "Sent to my phone", in "Got it from my phone" with buttons Open (`file:///…`) and Show in folder (`file:///…folder`); failed "Couldn't send <label> to <peer>"; cancelled "Cancelled"; the progress toast has a Cancel button `argus-send://cancel/<offer_id>`. Icon: `helios/public/icon-192.png` resolved from the checkout.

- [ ] **Step 1: Write the failing tests** (pure, run everywhere):

```python
def test_progress_toast_binds_its_values():
    x = progress_xml("Receiving from my phone", "Home Lab review.pdf")
    assert '<progress value="{progressValue}"' in x and 'status="{progressStatus}"' in x
    assert 'valueStringOverride="{progressValueString}"' in x and "Home Lab review.pdf" in x

def test_result_toast_escapes_and_launches_by_protocol():
    x = result_xml("Got it from my phone", "a<b>.pdf", [("Open", "file:///C:/x/a.pdf")])
    assert "a&lt;b&gt;.pdf" in x and 'activationType="protocol"' in x and 'arguments="file:///C:/x/a.pdf"' in x

def test_values_show_percent_speed_and_time_left():
    p = Progress("o1", "send", "pc", "phone", "a", 1, 100_000_000, now=0.0)
    for i in range(1, 9):
        p.update(4_100_000 * i // 2, now=0.5 * i)
    v = progress_values(p)
    assert v["progressValueString"].endswith("%") and "MB/s" in v["progressStatus"] and "left" in v["progressStatus"]
```

  Windows-only (`pytest.mark.skipif(sys.platform != "win32")`): `register_app` writes the key under a test-only id (parameter `app_id`) and removes it; `Toaster().show()` with a real `Progress` doesn't raise.
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement** `toast.py`; wire `SendAgent.on_progress = toaster.show` (only when `sys.platform == "win32"` and running as the agent), `cli.py` `_problem` → `Toaster().problem`, the queued toast is dropped (the progress toast appears within a second); `cli.py` gains `cancel-url <url>` parsing `argus-send://cancel/<offer_id>`; the agent calls `register_app` and `register_cancel_protocol` once at start.
- [ ] **Step 4: Run all Send tests; lint** — `pytest tests/test_send_*.py`, `ruff check core tests`
- [ ] **Step 5: Install and check for real**: `pip install -e ".[send]"` in `.venv`; run `argus send phone <a 200 MB file>` from `C:\Windows\System32`; see one "Argus" notification fill and turn into "Sent to my phone"; press Cancel on a second send and see "Cancelled". Restart the "Argus Send agent" task first (Sas approved restarting it earlier for this flow).
- [ ] **Step 6: Commit** `feat(send): the Argus notification with a live progress bar`

### Task 5: Ship

- [ ] `dev.ps1 pr`, then `dev.ps1 merge local`; confirm local `main` matches GitHub; restart the "Argus Send agent" task so it runs the new code (the supervisor restarts the worker and desk processes on its own; the Send agent is its own task).
