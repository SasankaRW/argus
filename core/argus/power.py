"""The power manager: the PC is woken when there is GPU or desktop work, and shut down when there is none.

While developing on the PC it is **simulated**: it decides exactly as the real one will, and only logs and
records `power.would_wake` / `power.would_shutdown` (visible on the Helios map), so the rules can be watched for
a while before they ever switch a computer off. The real mode (Wake-on-LAN from the laptop, shutdown through the
desktop runner, with a Cancel button on the phone) comes with the laptop deployment.

Rules:
- **PC work** is any job whose needs include one of `power.pc_needs` (gpu, desktop).
- **Wake** when PC work is queued and no online worker can take it. Said again at most every 10 minutes.
- **Shut down** when there has been no PC work (queued, leased, running) for `power.idle_minutes`. Said once
  per idle stretch; new PC work starts a new stretch. (Keyboard and mouse idleness is checked by the desktop
  runner in the real mode.)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from .config import Config
from .db import Store
from .events import insert_event
from .registry import _upsert_component

log = logging.getLogger("argus.power")

WAKE_REPEAT = 600.0


class PowerManager:
    def __init__(self, store: Store, cfg: Config, clock: Callable[[], float] = time.time):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.idle_since: float | None = None
        self.shutdown_said = False
        self.last_wake: float = 0.0
        self.state = "unknown"  # busy, idle, would_shutdown

    def _pc_work(self, conn: sqlite3.Connection) -> tuple[int, int]:
        """(PC jobs waiting to start, PC jobs running)."""
        needs = self.cfg.power.pc_needs
        waiting = running = 0
        for r in conn.execute("SELECT state, needs FROM jobs WHERE state IN ('queued','retry','leased','running')"):
            if set(json.loads(r["needs"])) & set(needs):
                if r["state"] in ("leased", "running"):
                    running += 1
                else:
                    waiting += 1
        return waiting, running

    def _pc_online(self, conn: sqlite3.Connection) -> bool:
        needs = set(self.cfg.power.pc_needs)
        for r in conn.execute("SELECT capabilities FROM workers WHERE state = 'online'"):
            if set(json.loads(r["capabilities"])) & needs:
                return True
        return False

    async def start(self) -> None:
        await self.store.write(lambda c: self._set(c, self.clock(), "unknown"))

    def _set(self, conn: sqlite3.Connection, now: float, state: str, **meta: Any) -> None:
        self.state = state
        _upsert_component(conn, now, "power", "service", "Power", None,
                          {"mode": self.cfg.power.mode, "state": state, "idle_minutes": self.cfg.power.idle_minutes,
                           **meta})

    async def tick(self) -> str:
        """Look once (on the watchdog). Returns the state: busy, idle or would_shutdown."""

        def fn(conn: sqlite3.Connection) -> str:
            now = self.clock()
            waiting, running = self._pc_work(conn)
            online = self._pc_online(conn)
            if waiting and not online and now - self.last_wake >= WAKE_REPEAT:
                self.last_wake = now
                insert_event(conn, now, "power.would_wake", src="power", dst="argus",
                             data={"waiting": waiting, "mode": self.cfg.power.mode})
                log.info("would wake the PC (simulated)", extra={"waiting_jobs": waiting})
            if waiting or running:
                if self.state != "busy":
                    self._set(conn, now, "busy")
                self.idle_since, self.shutdown_said = None, False
                return "busy"
            if self.idle_since is None:
                self.idle_since = now
                self._set(conn, now, "idle", idle_since=now)
            idle_for = now - self.idle_since
            if not self.shutdown_said and idle_for >= self.cfg.power.idle_minutes * 60:
                self.shutdown_said = True
                self._set(conn, now, "would_shutdown", idle_since=self.idle_since)
                insert_event(conn, now, "power.would_shutdown", src="power", dst="argus",
                             data={"idle_minutes": round(idle_for / 60, 1), "mode": self.cfg.power.mode})
                log.info("would shut down the PC (simulated)", extra={"idle_minutes": round(idle_for / 60, 1)})
            return "would_shutdown" if self.shutdown_said else "idle"

        return await self.store.write(fn)

    def status(self) -> dict[str, Any]:
        return {"mode": self.cfg.power.mode, "state": self.state, "idle_since": self.idle_since,
                "idle_minutes": self.cfg.power.idle_minutes}
