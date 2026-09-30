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

import asyncio
import json
import logging
import socket
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from .config import Config
from .db import Store
from .events import insert_event
from .outbox import add_message, ntfy_message
from .registry import _upsert_component

log = logging.getLogger("argus.power")

STALE_POWER_JOB = 300  # seconds a power button's job may wait for an offline PC
WAKE_REPEAT = 600.0


class PowerError(Exception):
    pass


class PowerManager:
    def __init__(self, store: Store, cfg: Config, clock: Callable[[], float] = time.time):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.idle_since: float | None = None
        self.shutdown_said = False
        self.last_wake: float = 0.0
        self.state = "unknown"  # busy, idle, warned, shutting_down, held, would_shutdown
        self.was_online = False
        self.by_argus = False  # this PC session was started by Argus's wake (only those shut down on their own)
        self.warned_at: float | None = None
        self.held = False
        self.jobs = None  # JobStore, set by Argus: the shutdown is a job for the PC's worker

    def _pc_work(self, conn: sqlite3.Connection) -> tuple[int, int]:
        """(PC jobs waiting to start, PC jobs running). The power buttons' own jobs don't count: a queued shutdown
        must not wake the PC or keep it "busy"."""
        needs = self.cfg.power.pc_needs
        waiting = running = 0
        for r in conn.execute("SELECT state, needs FROM jobs WHERE state IN ('queued','retry','leased','running') "
                              "AND plugin != 'power'"):
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
        """Look once (on the watchdog). Returns the state: busy, idle, warned, shutting_down, held,
        would_shutdown (simulated)."""
        real = self.cfg.power.mode == "real"
        send_wake = False

        def fn(conn: sqlite3.Connection) -> str:
            nonlocal send_wake
            now = self.clock()
            waiting, running = self._pc_work(conn)
            online = self._pc_online(conn)
            if online and not self.was_online:  # the PC just came on: did we wake it?
                self.by_argus = now - self.last_wake < 900
                insert_event(conn, now, "power.pc_online", src="pc", dst="power",
                             data={"woken_by_argus": self.by_argus})
            self.was_online = online
            if not online and self.jobs is not None:  # a power job for a PC that went off must not run at next boot
                for r in conn.execute("SELECT id FROM jobs WHERE plugin = 'power' AND state IN ('queued','retry') "
                                      "AND created_at < ?", (now - STALE_POWER_JOB,)):
                    self.jobs.cancel_in(conn, r["id"], "the PC went off before it ran")
            if waiting and not online and now - self.last_wake >= WAKE_REPEAT:
                self.last_wake = now
                if real and self.cfg.power.pc_mac:
                    send_wake = True
                    insert_event(conn, now, "power.wake_sent", src="power", dst="pc", data={"waiting": waiting})
                else:
                    insert_event(conn, now, "power.would_wake", src="power", dst="argus",
                                 data={"waiting": waiting, "mode": self.cfg.power.mode})
                    log.info("would wake the PC (simulated)", extra={"waiting_jobs": waiting})
            if waiting or running:
                if self.state != "busy":
                    self._set(conn, now, "busy")
                self.idle_since, self.shutdown_said, self.warned_at, self.held = None, False, None, False
                return "busy"
            if self.idle_since is None:
                self.idle_since = now
                self._set(conn, now, "idle", idle_since=now)
            if self.held:
                return "held"
            idle_for = now - self.idle_since
            if self.shutdown_said or idle_for < self.cfg.power.idle_minutes * 60:
                return self.state if self.shutdown_said else "idle"
            if not real:
                self.shutdown_said = True
                self._set(conn, now, "would_shutdown", idle_since=self.idle_since)
                insert_event(conn, now, "power.would_shutdown", src="power", dst="argus",
                             data={"idle_minutes": round(idle_for / 60, 1), "mode": self.cfg.power.mode})
                log.info("would shut down the PC (simulated)", extra={"idle_minutes": round(idle_for / 60, 1)})
                return "would_shutdown"
            # real: only a PC Argus woke (unless allowed for any session), and only after a warning on the phone
            if not online or not (self.by_argus or self.cfg.power.shutdown_manual_sessions):
                return "idle"
            warn = self.cfg.power.warn_minutes * 60
            if self.warned_at is None:
                self.warned_at = now
                at = time.strftime("%H:%M", time.localtime(now + warn))
                self._set(conn, now, "warned", idle_since=self.idle_since, shutdown_at=now + warn)
                insert_event(conn, now, "power.shutdown_warned", src="power", dst="pc", data={"at": now + warn})
                link = (self.cfg.approvals.public_url or "").rstrip("/")
                add_message(conn, now, "ntfy", ntfy_message(
                    f"PC shuts down at {at}",
                    f"Idle for {round(idle_for / 60)} min. Open Helios > Power to keep it on.",
                    tags=["zzz"], click=f"{link}/helios/#power" if link else None), dedupe_key=f"power-warn:{int(now)}")
                return "warned"
            if now - self.warned_at >= warn and self.jobs is not None:
                self.shutdown_said = True
                self.jobs.enqueue_in(conn, now, "power", "auto_shutdown",
                                     {"idle_minutes": self.cfg.power.idle_minutes,
                                      "delay": self.cfg.power.shutdown_delay_seconds},
                                     needs=["desktop"], priority=100, dedupe_key="power:auto_shutdown", source="power")
                self._set(conn, now, "shutting_down", idle_since=self.idle_since)
                return "shutting_down"
            return "warned"

        state = await self.store.write(fn)
        if send_wake:
            try:
                await asyncio.to_thread(self.wake)
            except (PowerError, OSError) as e:
                log.warning("could not send wake-on-lan", extra={"error": str(e)})
        return state

    async def hold(self) -> None:
        """Cancel pressed: no automatic shutdown for this idle stretch (it starts over when the PC has work)."""
        def fn(conn: sqlite3.Connection) -> None:
            self.held, self.warned_at = True, None
            self._set(conn, self.clock(), "held", idle_since=self.idle_since)
            insert_event(conn, self.clock(), "power.held", src="helios", dst="power")

        await self.store.write(fn)

    def wake(self) -> dict[str, Any]:
        """Send the Wake-on-LAN magic packet to the PC (from this machine; it must be on the PC's network)."""
        mac = self.cfg.power.pc_mac
        if not mac:
            raise PowerError("set power.pc_mac in argus.yaml first (the PC's wired network card: ipconfig /all)")
        packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, (self.cfg.power.wol_broadcast, self.cfg.power.wol_port))
        log.info("wake-on-lan sent", extra={"mac": mac})
        return {"sent": True, "mac": mac}

    def status(self) -> dict[str, Any]:
        return {"mode": self.cfg.power.mode, "state": self.state, "idle_since": self.idle_since,
                "idle_minutes": self.cfg.power.idle_minutes, "woken_by_argus": self.by_argus,
                "shutdown_at": self.warned_at + self.cfg.power.warn_minutes * 60 if self.warned_at else None}
