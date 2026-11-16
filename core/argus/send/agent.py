"""The Send agent: receiver + sender + queue loop + Argus link, run together (`argus send-agent`)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import uvicorn

from ..config import SendConfig
from .devices import DeviceBook
from .discovery import Discovery, local_addresses
from .keys import DeviceKey
from .receiver import build_app
from .sender import Sender, label_of
from .store import QueueStore

log = logging.getLogger("argus.send")
POLL_SECONDS = 2.0  # how often the queue is looked at (the CLI writes to it from outside)


def parse_online(status: dict) -> set[str]:
    """Host names that `tailscale status --json` shows online."""
    peers = (status.get("Peer") or {}).values()
    return {str(p.get("HostName", "")).lower() for p in peers if p.get("Online")} - {""}


def tailscale_online() -> set[str]:
    try:
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=10, check=True).stdout
        return parse_online(json.loads(out))
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()


class SendAgent:
    def __init__(self, cfg: SendConfig, *, argus_token: str | None = None, transport=None):
        if not cfg.device_id:
            raise ValueError("send.device_id is not set")
        self.cfg = cfg
        self.data = Path(cfg.data_dir or Path.home() / ".argus-send")
        self.root = Path(cfg.root or Path.home() / "Argus Shared")
        self.key = DeviceKey(self.data / "device.key")
        self.devices = DeviceBook(self.data / "devices.json")
        self.store = QueueStore(self.data / "send-queue.db")
        self.discovery = Discovery(cfg.device_id, cfg.port, self.wake, cfg.mdns)
        self.sender = Sender(self, transport)
        self.app = build_app(self)
        self.argus_token = argus_token
        self.argus_up = False
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._server: uvicorn.Server | None = None
        self._argus_client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ what other parts call
    def me(self) -> dict:
        c = self.cfg
        return {"id": c.device_id, "name": c.name or c.device_id, "pubkey": self.key.pub, "lan": c.lan,
                "tailscale": c.tailscale}

    def wake(self) -> None:
        """A retry trigger (network changed, target announced, Argus is back, new share): try waiting items now."""
        self.store.kick()
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)

    def enqueue(self, target: str, paths: list[str | Path]) -> str:
        if not self.devices.get(target):
            raise ValueError(f"unknown device '{target}'")
        manifest: list[dict] = []
        for p in map(Path, paths):
            p = p.resolve()
            if p.is_dir():
                for dirpath, _dirs, names in os.walk(p):
                    for name in sorted(names):
                        fp = Path(dirpath) / name
                        manifest.append(self._entry(fp, (Path(p.name) / fp.relative_to(p)).as_posix()))
            elif p.is_file():
                manifest.append(self._entry(p, p.name))
            else:
                raise ValueError(f"not found: {p}")
        if not manifest:
            raise ValueError("nothing to send")
        for n, f in enumerate(manifest):
            f["n"] = n
        oid = self.store.add(target, manifest)  # on disk before any byte moves
        self.wake()
        return oid

    @staticmethod
    def _entry(path: Path, rel: str) -> dict:
        st = path.stat()
        return {"abs": str(path), "rel_path": rel, "size": st.st_size, "mtime": st.st_mtime, "sha256": None}

    def received(self, offer: dict, saved: list[str]) -> None:
        origin = offer.get("origin") or offer["from"]
        self.store.event("received", peer=self.devices.name_of(origin), label=label_of(offer["files"]),
                         nbytes=offer["total"], offer_id=offer["offer_id"])
        self._flush_soon()

    def accept_hold(self, offer: dict) -> None:
        """A share arrived for a device that is offline: queue its delivery like any sender would."""
        oid = offer["offer_id"]
        d = self.root / ".holding" / oid
        manifest = [{"abs": str(d / f"{f['n']}.data"), "rel_path": f["rel_path"], "size": f["size"],
                     "mtime": f["mtime"], "sha256": f["sha256"], "n": f["n"]} for f in offer["files"]]
        with contextlib.suppress(sqlite3.IntegrityError):
            self.store.add(offer["hold_for"], manifest, offer_id=oid, kind="hold",
                           origin=offer.get("origin") or offer["from"])
        self.store.event("held", peer=self.devices.name_of(offer["hold_for"]), label=label_of(manifest),
                         nbytes=offer["total"], detail=f"from {self.devices.name_of(offer['from'])}", offer_id=oid)
        self.wake()

    def mark_delivered(self, oid: str, by: str) -> bool:
        """The holder says our held share reached the target: held -> done."""
        item = self.store.get(oid)
        if not item or item["state"] != "held" or item["holder"] != by or item["kind"] != "send":
            return False
        self.store.set(oid, state="done", reason=None, sent=item["total"])
        self.store.event("sent", peer=self.devices.name_of(item["target"]), label=label_of(item["manifest"]),
                         nbytes=item["total"], detail=f"delivered by {self.devices.name_of(by)}", offer_id=oid)
        return True

    def cancel_hold(self, oid: str) -> None:
        item = self.store.get(oid)
        if item and item["kind"] == "hold" and item["state"] in ("queued", "held", "sending"):
            self.store.set(oid, state="cancelled", reason="the sender reached the target directly")

    def drop_hold_files(self, oid: str) -> None:
        shutil.rmtree(self.root / ".holding" / oid, ignore_errors=True)

    def sweep_holding(self, now: float | None = None) -> list[str]:
        """Held shares older than holding_days are dropped (the sender is told in its history); stale partials too."""
        now = now or time.time()
        gone: list[str] = []
        base = self.root / ".holding"
        for d in base.iterdir() if base.exists() else []:
            try:
                meta = json.loads((d / "offer.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            age = now - meta.get("created", now)
            if age > self.cfg.holding_days * 86400 or (not meta.get("complete") and age > 86400):
                shutil.rmtree(d, ignore_errors=True)
                item = self.store.get(d.name)
                if item and item["state"] in ("queued", "held"):
                    self.store.set(d.name, state="failed", reason="held too long, not delivered")
                    self.store.event("not_delivered", peer=self.devices.name_of(meta.get("hold_for") or ""),
                                     label=label_of(meta["files"]), detail=f"from {self.devices.name_of(meta['from'])}",
                                     offer_id=d.name)
                gone.append(d.name)
        inc = self.root / ".incoming"
        for d in inc.iterdir() if inc.exists() else []:
            if now - d.stat().st_mtime > self.cfg.keep_waiting_days * 86400:
                shutil.rmtree(d, ignore_errors=True)
        return gone

    # ------------------------------------------------------------------ Argus (optional)
    def _flush_soon(self) -> None:
        if self._loop and self.cfg.argus_url:
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.argus_cycle(events_only=True)))

    async def argus_cycle(self, events_only: bool = False) -> None:
        """Register, refresh the device list, flush the outbox. Anything wrong just leaves things as they are."""
        if not self.cfg.argus_url:
            return
        if self._argus_client is None:
            self._argus_client = httpx.AsyncClient(timeout=5)
        c = self._argus_client
        base = self.cfg.argus_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.argus_token}"} if self.argus_token else {}
        try:
            if not events_only:
                (await c.post(base + "/send/devices", json=self.me(), headers=headers)).raise_for_status()
                r = await c.get(base + "/send/devices", headers=headers)
                r.raise_for_status()
                self.devices.update(r.json(), keep=self.cfg.device_id)
            sent = []
            for rid, ev in await asyncio.to_thread(self.store.outbox):
                (await c.post(base + "/send/events", json={**ev, "device": self.cfg.device_id},
                              headers=headers)).raise_for_status()
                sent.append(rid)
            self.store.outbox_done(sent)
            if any(i["state"] == "held" for i in self.store.active()):
                r = await c.get(base + "/send/history?limit=100", headers=headers)
                r.raise_for_status()
                for ev in r.json():  # the holder's "delivered", for when it could not reach us itself
                    if ev.get("kind") == "delivered" and ev.get("offer_id") and ev["device"] != self.cfg.device_id:
                        self.mark_delivered(ev["offer_id"], ev["device"])
            if not self.argus_up:
                self.argus_up = True
                self.wake()  # Argus is reachable again: a retry trigger
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.debug("Argus not reachable: %s", e)
            self.argus_up = False

    # ------------------------------------------------------------------ loops
    async def _queue_loop(self) -> None:
        while True:
            for item in await asyncio.to_thread(self.store.due, time.time()):
                try:
                    await self.sender.process(item)
                except Exception:  # noqa: BLE001  one bad entry must not stop the queue
                    log.exception("send: %s failed unexpectedly", item["id"])
                    self.store.set(item["id"], reason="internal error, see the log", next_try=time.time() + 60)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
            self._wake.clear()

    async def _argus_loop(self) -> None:
        while True:
            await self.argus_cycle()
            await asyncio.sleep(self.cfg.register_seconds)

    async def _net_loop(self) -> None:
        seen = local_addresses()
        while True:
            await asyncio.sleep(5)
            now = local_addresses()
            if now != seen:
                seen = now
                self.wake()  # the network changed

    async def _tailscale_loop(self) -> None:
        """A device showing up online on Tailscale is a retry trigger. No tailscale binary: nothing to do."""
        if not shutil.which("tailscale"):
            return
        online: set[str] = set()
        while True:
            now = await asyncio.to_thread(tailscale_online)
            if now - online:
                self.wake()
            online = now
            await asyncio.sleep(30)

    async def _hold_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.sweep_holding)
            await asyncio.sleep(600)

    # ------------------------------------------------------------------ start and stop
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.store.reset_interrupted()
        self.root.mkdir(parents=True, exist_ok=True)
        conf = uvicorn.Config(self.app, host=self.cfg.host, port=self.cfg.port, log_level="warning",
                              access_log=False, lifespan="off")
        self._server = uvicorn.Server(conf)
        serve = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if serve.done():
                serve.result()
                raise RuntimeError("the Send receiver did not start")
            await asyncio.sleep(0.02)
        self._tasks = [serve, asyncio.create_task(self._queue_loop()), asyncio.create_task(self._net_loop())]
        if self.cfg.argus_url:
            self._tasks.append(asyncio.create_task(self._argus_loop()))
        if self.cfg.tailscale:  # only a device that uses Tailscale polls it
            self._tasks.append(asyncio.create_task(self._tailscale_loop()))
        if self.cfg.holding:
            self._tasks.append(asyncio.create_task(self._hold_loop()))
        await asyncio.to_thread(self.discovery.start)

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        for t in self._tasks[1:]:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.to_thread(self.discovery.stop)
        await self.sender.close()
        if self._argus_client:
            await self._argus_client.aclose()
        self.store.close()

    async def run_forever(self) -> None:
        await self.start()
        log.info("Argus Send: %s listening on %s:%s, files go to %s", self.cfg.device_id, self.cfg.host,
                 self.cfg.port, self.root)
        try:
            await asyncio.gather(*self._tasks)
        finally:
            await self.stop()
