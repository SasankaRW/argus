"""Your apps on the live map: Argus asks Life Hub and Tracker whether they answer, every 2 minutes.

An app is checked when its plugin (`lifehub`, `tracker`) is enabled and has a `url` setting; otherwise it has no box.
The rules (when it counts as down, since when, what its one line says) are pure functions here so they can be tested
without a network. `AppHealth` does the asking (GET <url>/api/health, 5 s) and keeps the answers for the map.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

INTERVAL = 120.0  # seconds between checks
TIMEOUT = 5.0
DOWN_AFTER = 2  # consecutive failures before an app counts as down

# plugin id -> (map node id, name, the .env name of its API key)
APPS = {
    "lifehub": ("app-lifehub", "Life Hub", "LIFEHUB_API_KEY"),
    "tracker": ("app-tracker", "Tracker", "TRACKER_API_KEY"),
}


@dataclass(frozen=True)
class State:
    up: bool | None = None  # None: not asked yet
    since: float | None = None  # up: since the first good answer; down: the first failure of the streak
    fails: int = 0  # consecutive failures
    error: str = ""  # the last error
    checked: float | None = None
    line: str = ""  # one line of status, from the last good answer


def step(state: State, ok: bool, now: float, error: str = "", line: str | None = None) -> State:
    """The state after one check: down only after DOWN_AFTER failures in a row, `since` the first failure."""
    if ok:
        since = state.since if state.up and state.fails == 0 else now
        return State(True, since, 0, "", now, state.line if line is None else line)
    fails = state.fails + 1
    since = now if fails == 1 else (state.since or now)
    return replace(state, up=state.up if fails < DOWN_AFTER else False, since=since, fails=fails, error=error[:200],
                   checked=now)


def is_down(state: State) -> bool:
    return state.up is False and state.fails >= DOWN_AFTER


def life_hub_line(news: list[dict] | None) -> str:
    """'N back in stock' (what the wishlist banner shows), 'N wishlist updates' for price drops only, 'All synced'."""
    if news is None:
        return "Up"
    back = sum(1 for n in news if n.get("kind") == "back_in_stock")
    if back:
        return f"{back} back in stock"
    return f"{len(news)} wishlist update{'s' if len(news) != 1 else ''}" if news else "All synced"


def tracker_line(summary: dict | None) -> str:
    n = (summary or {}).get("open")
    if not isinstance(n, int):
        return "Up"
    return "No open issues" if n == 0 else f"{n} open issue{'s' if n != 1 else ''}"


def node_fields(state: State) -> dict[str, Any]:
    """What the map's node says (see Registry.map)."""
    if state.up is None:
        return {"status": "checking", "line": "Checking", "since": None, "error": "", "fails": 0}
    down = is_down(state)
    return {"status": "down" if down else "up", "line": "Not answering" if down else (state.line or "Up"),
            "since": state.since if down else None, "error": state.error if down else "", "fails": state.fails}


Fetch = Callable[[str, dict[str, str]], Any]


def fetch_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - the address you set for the plugin
        raw = r.read()
    return json.loads(raw) if raw else None


def check_one(plugin: str, base: str, key: str, fetch: Fetch = fetch_json) -> tuple[bool, str, str | None]:
    """(answers, error, one line). /api/health decides up or down; the line comes from a second, keyed call."""
    base = base.rstrip("/")
    try:
        fetch(base + "/api/health", {})
    except Exception as e:  # noqa: BLE001 - refused, timeout, bad HTTP status: all the same to the map
        return False, str(e) or type(e).__name__, None
    line = "Up"
    if key:
        try:
            if plugin == "lifehub":
                line = life_hub_line(fetch(base + "/api/advisor/news", {"X-API-Key": key}))
            else:
                line = tracker_line(fetch(base + "/api/summary", {"X-API-Key": key}))
        except Exception:  # noqa: BLE001 - it answers, just not this
            line = "Up"
    return True, "", line


class AppHealth:
    def __init__(self, targets: Callable[[], dict[str, tuple[str, str]]], clock: Callable[[], float] = time.time,
                 fetch: Fetch = fetch_json, interval: float = INTERVAL):
        """`targets()`: {plugin id: (url, api key)} for the apps whose plugin is on and has a url."""
        self.targets = targets
        self.clock = clock
        self.fetch = fetch
        self.interval = interval
        self.states: dict[str, State] = {}
        self._next = 0.0
        self._task: asyncio.Task | None = None

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def tick(self) -> None:
        """Called by the watchdog: starts a check when one is due and returns at once (the network is slow)."""
        now = self.clock()
        if now < self._next or (self._task and not self._task.done()):
            return
        self._next = now + self.interval
        self._task = asyncio.create_task(self.check(), name="argus-app-health")

    async def check(self) -> None:
        targets = self.targets()
        self.states = {p: s for p, s in self.states.items() if p in targets}  # a plugin turned off: no box
        results = await asyncio.gather(*[asyncio.to_thread(check_one, p, url, key, self.fetch)
                                         for p, (url, key) in targets.items()])
        now = self.clock()
        for p, (ok, err, line) in zip(targets, results, strict=True):
            self.states[p] = step(self.states.get(p, State()), ok, now, err, line)

    def nodes(self) -> list[dict[str, Any]]:
        """The app nodes the map adds (only for apps whose plugin is on and has a url)."""
        try:
            wanted = self.targets()
        except Exception:  # noqa: BLE001 - the map must never fail over this
            return []
        out = []
        for pid, (nid, name, _key) in APPS.items():
            if pid not in wanted:
                continue
            s = self.states.get(pid, State())
            out.append({"id": nid, "kind": "app", "label": name, "group": None, "meta": {},
                        "plugin": pid, "first_seen": s.since or self.clock(), "every": int(self.interval),
                        **node_fields(s)})
        return out
