"""Phone presence: when your phone comes back on Tailscale, push the approvals still waiting.

The Approve / Reject buttons go straight to Argus over Tailscale, so a tap while the phone is off Tailscale does
nothing (the approval simply stays waiting). Every `approvals.presence_seconds` this watcher asks the local
Tailscale client (`tailscale status --json`) whether the phone (`approvals.phone`) is online. When it goes from
offline to online and something is waiting, one message is sent with fresh buttons. At most one such push per
`back_online_cooldown_minutes`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .approvals import Approvals
from .config import Config
from .db import Store
from .events import insert_event
from .outbox import Outbox
from .registry import _upsert_component

log = logging.getLogger("argus.presence")

WINDOWS_CLI = Path("C:/Program Files/Tailscale/tailscale.exe")


def phone_online(status: dict[str, Any], name: str) -> bool | None:
    """Is the device called `name` online? None when the tailnet has no such device."""
    want = name.lower().rstrip(".")
    for peer in (status.get("Peer") or {}).values():
        host = str(peer.get("HostName", "")).lower()
        dns = str(peer.get("DNSName", "")).lower().rstrip(".")
        if want in (host, dns) or dns.split(".")[0] == want:
            return bool(peer.get("Online"))
    return None


PRIVATE = re.compile(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[?fd|\[?fe80)")


def phone_info(status: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Where the phone is, as far as Tailscale can tell: online, last seen, and home or away (a direct connection
    over a home-network address means it is on the same Wi-Fi as this machine). None: no such device."""
    want = name.lower().rstrip(".")
    for peer in (status.get("Peer") or {}).values():
        host = str(peer.get("HostName", "")).lower()
        dns = str(peer.get("DNSName", "")).lower().rstrip(".")
        if want not in (host, dns) and dns.split(".")[0] != want:
            continue
        online = bool(peer.get("Online"))
        addr = str(peer.get("CurAddr") or "")
        if not online:
            where = "unknown"
        elif addr and PRIVATE.match(addr):
            where = "home"
        else:
            where = "away"
        last = str(peer.get("LastSeen") or "")
        return {"device": peer.get("HostName") or name, "online": online, "where": where,
                "last_seen": None if last.startswith("0001") else last or None,
                "via": "direct" if addr else (f"relay {peer.get('Relay')}" if peer.get("Relay") else None)}
    return None


def describe_phone(info: dict[str, Any] | None) -> str:
    if info is None:
        return "I can't see your phone on Tailscale (check approvals.phone in argus.yaml)."
    if info["online"]:
        return {"home": "Your phone is online and at home (on the same Wi-Fi as the PC).",
                "away": "Your phone is online but not at home (not on the home Wi-Fi)."}.get(
            info["where"], "Your phone is online.")
    seen = f" It was last online {info['last_seen'][:16].replace('T', ' ')} UTC." if info.get("last_seen") else ""
    return "Your phone is offline on Tailscale (off, asleep with no data, or Tailscale is off)." + seen


class PhoneWatch:
    def __init__(self, store: Store, cfg: Config, approvals: Approvals, outbox: Outbox,
                 clock: Callable[[], float] = time.time, status: Callable[[], dict] | None = None):
        self.store = store
        self.cfg = cfg
        self.approvals = approvals
        self.outbox = outbox
        self.clock = clock
        self._status = status or self._tailscale_status
        self._task: asyncio.Task | None = None
        self.online: bool | None = None  # last seen state; None = not known yet
        self.last_push = 0.0
        self.pushes = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        a = self.cfg.approvals
        return bool(a.phone)

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not self.enabled:
            return
        await self._save(None)
        self._task = asyncio.create_task(self._loop(), name="argus-phone-watch")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def alive(self) -> bool:
        return not self.enabled or (self._task is not None and not self._task.done())

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # Tailscale not running, CLI missing: keep trying quietly
                if self.last_error != str(e):
                    log.warning("could not read tailscale status", extra={"error": str(e)})
                self.last_error = str(e)
            await asyncio.sleep(self.cfg.approvals.presence_seconds)

    # -------------------------------------------------------------- checking

    def _tailscale_status(self) -> dict:
        cmd = list(self.cfg.approvals.tailscale_command)
        exe = shutil.which(cmd[0]) or (str(WINDOWS_CLI) if WINDOWS_CLI.exists() else None)
        if exe is None:
            raise RuntimeError("tailscale CLI not found")
        r = subprocess.run([exe, *cmd[1:], "status", "--json"], capture_output=True, text=True, timeout=10,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"tailscale status failed: {r.stderr.strip()[:200]}")
        return json.loads(r.stdout)

    async def info(self) -> dict[str, Any] | None:
        """Where the phone is now (a fresh look at Tailscale)."""
        return phone_info(await asyncio.to_thread(self._status), self.cfg.approvals.phone or "")

    async def check(self) -> str:
        """One look. Returns what happened: online, offline, back (pushed), unknown."""
        status = await asyncio.to_thread(self._status)
        now_online = phone_online(status, self.cfg.approvals.phone or "")
        if now_online is None:
            self.last_error = f"no device named {self.cfg.approvals.phone!r} on the tailnet"
            return "unknown"
        was = self.online
        self.online = now_online
        if was != now_online:
            await self._save(now_online, event=was is not None)
        if now_online and was is False:
            return "back" if await self._push() else "online"
        return "online" if now_online else "offline"

    async def _save(self, online: bool | None, event: bool = False) -> None:
        """Keep the phone's box on the map current: online or offline, and since when."""
        name = self.cfg.approvals.phone

        def fn(conn) -> None:
            now = self.clock()
            meta = {"device": name, "online": online, "since": now if online is not None else None,
                    "via": "tailscale"}
            _upsert_component(conn, now, "phone", "service", "Phone", "tailscale", meta)
            if event:
                insert_event(conn, now, "phone.online" if online else "phone.offline", src="phone", dst="argus",
                             data={"device": name})

        await self.store.write(fn)

    async def _push(self) -> bool:
        now = self.clock()
        if now - self.last_push < self.cfg.approvals.back_online_cooldown_minutes * 60:
            return False
        n = await self.approvals.push_waiting(dedupe=f"back-online:{int(now)}")
        if n:
            self.last_push = now
            self.pushes += 1
            self.outbox.poke()
        return bool(n)

    def health(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "alive": self.alive, "phone_online": self.online, "pushes": self.pushes,
                "last_error": self.last_error}
