"""Live activities: what is happening right now (a file moving, a job running a while, a timer, a custom one from
the workout app or a plugin), in memory only. Activities are live state, so a restart clears them.

Sources: transfers are derived on read from `send_live`; jobs are promoted once they have run for PROMOTE_AFTER
seconds (fed by the job store's own events, no polling); timers come from a succeeded `phone.timer` job and from
`POST /activities/timer`; custom ones are put and deleted by whoever owns them and expire on their own.

Every change to what a client would see bumps `version`. `GET /activities?after=<version>&wait=25` sleeps until it
moves. The hub event `activity.changed` follows each bump, at most MIN_EMIT apart (the island wakes on it), and
`activity.done` fires once when a timer reaches its end.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("argus.activities")

PROMOTE_AFTER = 10.0  # a job is shown once it has run this long: quick jobs never flash
TIMER_LINGER = 5.0  # a finished timer stays this long so the end is seen
CUSTOM_GRACE = 60.0  # a custom activity with an end time goes this long after it
CUSTOM_IDLE = 600.0  # a custom activity without one goes after this long without an update
MIN_EMIT = 0.25  # activity.changed at most 4 times a second
SOON = 60.0  # a timer this close to its end beats everything
MAX_CUSTOM = 20
FINISHED = ("done", "failed", "cancelled")

PRIORITY = {"timer_soon": 5, "transfer": 4, "timer": 3, "job": 2, "custom": 1}


def priority_of(item: dict, now: float) -> int:
    """Higher is more urgent: a timer under a minute from its end, then transfers, timers, jobs, custom."""
    if item.get("kind") == "timer" and item.get("ends_at") is not None and item["ends_at"] - now < SOON:
        return PRIORITY["timer_soon"]
    return PRIORITY.get(str(item.get("kind")), 0)


def rank(items: list[dict], now: float) -> list[dict]:
    """Most urgent first; ties go to the newest. Returns copies carrying their `priority`."""
    out = [{**it, "priority": priority_of(it, now)} for it in items]
    return sorted(out, key=lambda a: (-a["priority"], -(a.get("started_at") or 0)))


def _eta_words(eta: float) -> str:
    return f"{max(1, round(eta))} s left" if eta < 60 else f"{round(eta / 60)} min left"


def transfer_activity(t: dict) -> dict:
    """One moving Send transfer as an activity."""
    to = str(t.get("to", ""))
    pct = round(100 * t["done"] / t["total"]) if t.get("total") else 0
    if t.get("state") == "verifying":
        detail = "checking"
    elif t.get("eta") is not None:
        detail = f"{pct}% · {_eta_words(float(t['eta']))}"
    else:
        detail = f"{pct}%"
    return {"id": f"transfer:{t.get('offer_id', '')}:{t.get('leg', 'send')}", "kind": "transfer",
            "title": f"{t.get('label') or 'Files'} to {to.capitalize()}", "detail": detail, "icon": "swap_horiz",
            "progress": (min(1.0, t["done"] / t["total"]) if t.get("total") else 0.0), "ends_at": None,
            "started_at": t.get("started"), "device": to or None, "priority": 0}


def _words(s: str) -> str:
    s = s.replace("-", " ").replace("_", " ").strip()
    return s[:1].upper() + s[1:]


class Activities:
    def __init__(self, send_live: Any = None, clock: Callable[[], float] = time.time):
        self.send_live = send_live
        self.clock = clock
        self.version = 0
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}  # running jobs, shown after PROMOTE_AFTER
        self._timers: dict[str, dict] = {}
        self._custom: dict[str, dict] = {}
        self._sig: Any = ()
        self._emitted = 0
        self._last_emit = float("-inf")
        self._done: list[dict] = []
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._pump: tuple[asyncio.AbstractEventLoop, asyncio.Event] | None = None

    # ------------------------------------------------------------------ sources

    def job_event(self, kind: str, job: Any, now: float, step: str | None = None, data: dict | None = None) -> None:
        """Called by the job store at each of its event points (any thread)."""
        with self._lock:
            if kind == "job.running":
                self._jobs[job.id] = {"id": job.id, "plugin": job.plugin, "workflow": job.workflow, "since": now,
                                      "step": None, "idx": None}
            elif kind == "step.running":
                j = self._jobs.get(job.id)
                if j is not None:
                    j["step"], j["idx"] = step, (data or {}).get("idx")
            elif kind.startswith("job."):
                self._jobs.pop(job.id, None)  # finished, retrying, parked or cancelled
                if kind == "job.succeeded" and job.plugin == "phone" and job.workflow == "timer":
                    inp = job.input if isinstance(job.input, dict) else {}
                    try:
                        minutes = int(inp.get("minutes"))
                    except (TypeError, ValueError):
                        minutes = 0
                    if 1 <= minutes <= 1440:
                        self._add_timer(f"timer:{job.id}", minutes, str(inp.get("label") or ""), "phone", now)
            else:
                return
        self.poke()

    def _add_timer(self, tid: str, minutes: int, label: str, device: str, now: float) -> dict:
        t = {"id": tid, "title": label.strip()[:80] or "Timer", "detail": f"{minutes} min timer",
             "started_at": now, "ends_at": now + minutes * 60, "device": device, "announced": False}
        self._timers[tid] = t
        return t

    def start_timer(self, minutes: int, label: str = "", device: str = "pc") -> dict:
        now = self.clock()
        with self._lock:
            t = self._add_timer(f"timer:pc-{int(now * 1000)}", minutes, label, device, now)
            item = self._timer_item(t)
        self.poke()
        return item

    def put_custom(self, cid: str, body: dict) -> dict:
        now = self.clock()
        with self._lock:
            old = self._custom.get(cid)
            if old is None and len(self._custom) >= MAX_CUSTOM:
                oldest = min(self._custom.values(), key=lambda c: c["updated"])
                del self._custom[oldest["cid"]]
            p = body.get("progress")
            self._custom[cid] = {
                "cid": cid, "title": str(body.get("title") or "")[:80], "detail": str(body.get("detail") or "")[:120],
                "icon": str(body.get("icon") or "bolt")[:40],
                "progress": None if p is None else max(0.0, min(1.0, float(p))),
                "ends_at": body.get("ends_at"), "device": body.get("device"),
                "started_at": old["started_at"] if old else now, "updated": now}
            item = self._custom_item(self._custom[cid])
        self.poke()
        return item

    def delete_custom(self, cid: str) -> bool:
        with self._lock:
            found = self._custom.pop(cid, None) is not None
        self.poke()
        return found

    # ------------------------------------------------------------------ items

    @staticmethod
    def _timer_item(t: dict) -> dict:
        return {"id": t["id"], "kind": "timer", "title": t["title"], "detail": t["detail"], "icon": "timer",
                "progress": None, "ends_at": t["ends_at"], "started_at": t["started_at"], "device": t["device"],
                "priority": 0}

    @staticmethod
    def _custom_item(c: dict) -> dict:
        return {"id": f"custom:{c['cid']}", "kind": "custom", "title": c["title"], "detail": c["detail"],
                "icon": c["icon"], "progress": c["progress"], "ends_at": c["ends_at"], "started_at": c["started_at"],
                "device": c["device"], "priority": 0}

    def _prune(self, now: float) -> None:
        for tid, t in list(self._timers.items()):
            if not t["announced"] and now >= t["ends_at"]:
                t["announced"] = True
                self._done.append({"id": tid, "kind": "timer", "title": t["title"]})
            if now > t["ends_at"] + TIMER_LINGER:
                del self._timers[tid]
        for cid, c in list(self._custom.items()):
            gone = (now > float(c["ends_at"]) + CUSTOM_GRACE) if c["ends_at"] is not None \
                else now - c["updated"] > CUSTOM_IDLE
            if gone:
                del self._custom[cid]

    def _build(self, now: float) -> list[dict]:
        items: list[dict] = []
        if self.send_live is not None:
            items += [transfer_activity(t) for t in self.send_live.items(now) if t.get("state") not in FINISHED]
        for j in self._jobs.values():
            if now - j["since"] >= PROMOTE_AFTER:
                detail = f"{j['step']} · step {int(j['idx']) + 1}" if j["step"] and j["idx"] is not None \
                    else (j["step"] or "starting")
                items.append({"id": f"job:{j['id']}", "kind": "job",
                              "title": f"{_words(j['plugin'])}: {j['workflow'].replace('_', ' ')}",
                              "detail": detail, "icon": "play_circle", "progress": None, "ends_at": None,
                              "started_at": j["since"], "device": None, "priority": 0})
        items += [self._timer_item(t) for t in self._timers.values()]
        items += [self._custom_item(c) for c in self._custom.values()]
        return rank(items, now)

    def tick(self, now: float | None = None) -> bool:
        """Expire, promote and notice changes; bumps the version (and wakes long polls) when what a client would
        see differs from last time."""
        now = self.clock() if now is None else now
        with self._lock:
            self._prune(now)
            items = self._build(now)
            # what changes the picture: not the transfer's eta text (its percent is in `progress`)
            sig = tuple((a["id"], a["priority"], a["title"], a["icon"], a["ends_at"], round(a["progress"] or 0, 2),
                         None if a["kind"] == "transfer" else a["detail"]) for a in items)
            if sig == self._sig:
                return False
            self._sig = sig
            self.version += 1
            waiters = list(self._waiters)
        self._wake(waiters)
        return True

    def snapshot(self, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        self.tick(now)
        with self._lock:
            return {"version": self.version, "items": self._build(now)}

    # ------------------------------------------------------------------ waking

    @staticmethod
    def _wake(waiters: list) -> None:
        for loop, ev in waiters:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:  # that loop has closed
                pass

    def poke(self) -> None:
        """Something moved: look now, and wake the event pump."""
        self.tick()
        with self._lock:
            pump = self._pump
        if pump is not None:
            self._wake([pump])

    async def changed(self, after: int, wait: float) -> dict:
        """The snapshot, once its version differs from `after` (or `wait` seconds pass)."""
        loop = asyncio.get_running_loop()
        end = loop.time() + max(0.0, wait)
        while True:
            snap = self.snapshot()
            left = end - loop.time()
            if snap["version"] != after or left <= 0:
                return snap
            ev = asyncio.Event()
            entry = (loop, ev)
            with self._lock:
                self._waiters.add(entry)
            try:
                if self.version != after:
                    continue
                try:
                    async with asyncio.timeout(min(1.0, left)):  # a look each second: expiry needs no event
                        await ev.wait()
                except TimeoutError:
                    pass
            finally:
                with self._lock:
                    self._waiters.discard(entry)

    # ------------------------------------------------------------------ hub events

    def take_events(self, now: float | None = None) -> list[tuple[str, dict]]:
        """The hub events due now: activity.changed (coalesced to one per MIN_EMIT) and each timer's activity.done."""
        now = self.clock() if now is None else now
        self.tick(now)
        out: list[tuple[str, dict]] = []
        with self._lock:
            for d in self._done:
                out.append(("activity.done", d))
            self._done = []
            if self.version != self._emitted and now - self._last_emit >= MIN_EMIT:
                self._emitted, self._last_emit = self.version, now
                out.append(("activity.changed", {"version": self.version}))
        return out

    def busy(self) -> bool:
        """Anything the pump needs to keep an eye on."""
        with self._lock:
            if self._jobs or self._timers or self._custom or self.version != self._emitted or self._done:
                return True
        return bool(self.send_live is not None and self.send_live.items(self.clock()))

    async def pump(self, write: Callable[[list[tuple[str, dict]]], Any]) -> None:
        """Run for the life of Argus: turn changes into hub events (`write` stores them)."""
        ev = asyncio.Event()
        with self._lock:
            self._pump = (asyncio.get_running_loop(), ev)
        while True:
            events = self.take_events()
            if events:
                try:
                    await write(events)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001  # boundary: one failed write must not end the pump
                    log.warning("activity events not written", exc_info=True)
            if self.busy():
                await asyncio.sleep(MIN_EMIT)
            else:
                ev.clear()
                try:
                    async with asyncio.timeout(5.0):
                        await ev.wait()
                except TimeoutError:
                    pass
