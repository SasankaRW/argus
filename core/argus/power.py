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
import ipaddress
import json
import logging
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from .config import Config
from .db import Store
from .events import insert_event
from .outbox import add_message, phone_message
from .registry import _upsert_component

log = logging.getLogger("argus.power")

STALE_POWER_JOB = 300  # seconds a power button's job may wait for an offline PC
WAKE_REPEAT = 600.0
WAKE_ANSWER_S = 150.0  # a woken PC should be online (its worker registered) within this long
WAKE_COPIES = 3  # magic packets are UDP: send each a few times
NOT_LAN = ("lo", "tailscale", "docker", "br-", "veth", "virbr", "wg", "zt", "tun", "tap")


def broadcasts_from(ip_json: list[dict]) -> list[tuple[str, str]]:
    """(this machine's address, its network's broadcast) for each home-network card, from `ip -j -4 addr`. Tailscale,
    Docker, VPNs and loopback are skipped: a wake packet must go out on the wire the PC is on."""
    out: list[tuple[str, str]] = []
    for card in ip_json:
        name = str(card.get("ifname") or "")
        if name.startswith(NOT_LAN) or "UP" not in (card.get("flags") or []):
            continue
        for a in card.get("addr_info") or []:
            if a.get("family") != "inet" or not a.get("local"):
                continue
            try:
                net = ipaddress.ip_interface(f"{a['local']}/{a.get('prefixlen', 24)}").network
            except ValueError:
                continue
            if not net.is_private or net.subnet_of(ipaddress.ip_network("100.64.0.0/10")) or net.prefixlen >= 31:
                continue
            out.append((str(a["local"]), str(a.get("broadcast") or net.broadcast_address)))
    return out


def lan_broadcasts() -> list[tuple[str, str]]:  # pragma: no cover - asks the system
    """The home-network cards of this machine (Linux: `ip`; elsewhere none, the configured address is used)."""
    if not sys.platform.startswith("linux"):
        return []
    try:
        p = subprocess.run(["ip", "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=5)
        return broadcasts_from(json.loads(p.stdout or "[]"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


WAKE_HINT = ("The PC didn't come online. On the PC: the network card's \"Wake on Magic Packet\" on, Windows Fast "
             "Startup off, a cable (not Wi-Fi); in the BIOS: Power On By PCI-E on, ErP off. "
             "dev.ps1 wol-check looks at the Windows side. Laptop on Wi-Fi: set power.pc_ip and run "
             "deploy/linux/pc-wake-setup.sh once, so the packet goes straight to the PC.")


def neighbour_of(ip: str) -> dict[str, Any] | None:  # pragma: no cover - asks the system
    """What this machine knows about `ip` on the home network (`ip -j neigh`): {mac, state, dev}, or None."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        p = subprocess.run(["ip", "-j", "neigh", "show", ip], capture_output=True, text=True, timeout=5)
        rows = json.loads(p.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for r in rows:
        if r.get("lladdr"):
            return {"mac": str(r["lladdr"]).upper(), "state": " ".join(r.get("state") or []), "dev": r.get("dev")}
    return None


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
        self.find_lans: Callable[[], list[tuple[str, str]]] = lan_broadcasts
        self.neighbour: Callable[[str], dict[str, Any] | None] = neighbour_of
        self.wake_check: dict[str, Any] | None = None  # the last wake: {at, to, result: waiting|online|no_answer}

    def _pc_work(self, conn: sqlite3.Connection) -> tuple[int, int]:
        """(PC jobs waiting to start, PC jobs running). The power buttons' own jobs don't count: a queued shutdown
        must not wake the PC or keep it "busy"."""
        needs = self.cfg.power.pc_needs
        waiting = running = 0
        for r in conn.execute("SELECT state, needs FROM jobs WHERE state IN ('queued','retry','leased','running') "
                              "AND plugin NOT IN ('power', 'health')"):
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
            wc = self.wake_check
            if wc is not None and wc["result"] == "waiting":
                if online:
                    wc["result"] = "online"
                    wc["after_s"] = round(now - wc["at"])
                elif now - wc["at"] > WAKE_ANSWER_S:
                    wc["result"] = "no_answer"
                    insert_event(conn, now, "power.wake_no_answer", src="power", dst="pc",
                                 data={"to": wc["to"], "hint": WAKE_HINT})
                    log.warning("the PC didn't wake", extra={"to": wc["to"]})
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
                add_message(conn, now, "phone", phone_message(
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
        """Send the Wake-on-LAN magic packet to the PC, a few times, out of every home-network card of this machine
        (to its network's broadcast) as well as to `power.wol_broadcast`. Then watch whether the PC comes online."""
        mac = self.cfg.power.pc_mac
        if not mac:
            raise PowerError("set power.pc_mac in argus.yaml first (the PC's wired network card: ipconfig /all)")
        packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16
        port = self.cfg.power.wol_port
        targets: list[tuple[str | None, str]] = [(None, self.cfg.power.wol_broadcast)]
        if self.cfg.power.wol_broadcast == "255.255.255.255":  # the default: also each card's own network
            targets += [(local, bcast) for local, bcast in self.find_lans()]
        direct = self.cfg.power.pc_ip
        if direct:  # straight to the PC's card (needs it in the neighbour table while the PC is off)
            targets.append((None, direct))
        sent: list[str] = []
        errors: list[str] = []
        for local, addr in targets:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    if local:
                        s.bind((local, 0))  # leave by that card, not whichever the routing table picks
                    for _ in range(WAKE_COPIES):
                        s.sendto(packet, (addr, port))
                sent.append(addr if local is None else f"{addr} (from {local})")
            except OSError as e:
                errors.append(f"{addr}: {e}")
        if not sent:
            raise OSError("; ".join(errors) or "no network to send on")
        now = self.clock()
        self.last_wake = now
        self.wake_check = {"at": now, "to": sent, "result": "waiting"}
        extra: dict[str, Any] = {}
        if direct:  # did the direct packet have the PC's card to go to? (the laptop forgets it while the PC is off)
            n = self.neighbour(direct)
            known = bool(n and n["mac"] == mac and "PERMANENT" in n["state"])
            extra["direct"] = "kept" if known else "not kept: run deploy/linux/pc-wake-setup.sh once (as root)"
            self.wake_check["direct"] = extra["direct"]
        log.info("wake-on-lan sent", extra={"mac": mac, "to": sent, "errors": errors, **extra})
        return {"sent": True, "mac": mac, "to": sent, **extra}

    def status(self) -> dict[str, Any]:
        return {"mode": self.cfg.power.mode, "state": self.state, "idle_since": self.idle_since,
                "wake_check": self.wake_check,
                "idle_minutes": self.cfg.power.idle_minutes, "woken_by_argus": self.by_argus,
                "shutdown_at": self.warned_at + self.cfg.power.warn_minutes * 60 if self.warned_at else None}
