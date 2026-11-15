"""Argus's own settings and power (the PC and the laptop)."""

from __future__ import annotations

import asyncio
import socket
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from ... import settings as settings_mod
from ...devices import laptop_power
from ...devices.power import PowerError
from ...events import insert_event
from ...outbox import add_message, phone_message
from ._deps import Deps, job_json


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    @router.get("/argus-settings", dependencies=guarded)
    async def argus_settings() -> dict:
        over = await argus.store.read(settings_mod.load)
        return {"settings": settings_mod.listing(argus.cfg, argus.base_settings, over)}

    @router.put("/argus-settings", dependencies=guarded)
    async def argus_settings_change(body: dict[str, Any]) -> dict:
        """{setting: value} changes it now and keeps it; {setting: null} goes back to argus.yaml."""
        try:
            await argus.change_settings(body)
        except settings_mod.SettingError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        return await argus_settings()

    @router.get("/power", dependencies=guarded)
    async def power() -> dict:
        """The power manager's view, whether the PC (a worker with desktop) is online, and recent power actions."""
        needs = set(argus.cfg.power.pc_needs)
        pcs = [w for w in await argus.registry.workers() if set(w["capabilities"]) & needs]
        online = [w for w in pcs if w["state"] == "online"]
        recent = [job_json(j) for j in await argus.jobs.list_jobs(None, 8, "power")]
        pending = [j for j in recent if j["state"] in ("queued", "retry", "leased", "running")]
        pc = (online or pcs or [None])[0]
        return {**argus.power.status(), "pc_online": bool(online),
                "pc": {k: pc.get(k) for k in ("id", "host", "state", "last_seen")} if pc else None,
                "wol": bool(argus.cfg.power.pc_mac), "delay": argus.cfg.power.shutdown_delay_seconds,
                "recent": recent, "pending": pending,
                "laptop": {"available": laptop_power.available(), "host": socket.gethostname(),
                           "scheduled": laptop_power.scheduled()}}

    @router.post("/power/laptop/{action}", dependencies=guarded)
    async def laptop_power_action(action: str, body: dict | None = None) -> dict:
        """The laptop's own buttons (Argus's server): restart-argus, reboot, shutdown (needs confirm), cancel."""
        try:
            out = await asyncio.to_thread(laptop_power.run, action, confirm=bool((body or {}).get("confirm")))
        except laptop_power.LaptopPowerError as e:
            raise HTTPException(status_code=409, detail=str(e)) from None
        now = time.time()

        def fn(c):
            insert_event(c, now, "power.laptop", src="helios", dst="power", data={"action": action})
            if action in ("reboot", "shutdown"):
                what = "restarts" if action == "reboot" else "shuts down"
                link = (argus.cfg.approvals.public_url or "").rstrip("/")
                add_message(c, now, "phone", phone_message(
                    f"Laptop {what} in 1 minute", "Argus will be away until it's back. Cancel in Helios > Power.",
                    tags=["zzz"], click=f"{link}/helios/#power" if link else None), dedupe_key=f"laptop:{int(now)}")
        await argus.store.write(fn)
        return out

    @router.post("/power/{action}", dependencies=guarded)
    async def power_action(action: str) -> dict:
        """The power buttons. wake: Wake-on-LAN from argusd. sleep / shutdown / restart / cancel: a job for the PC's
        worker, ahead of everything else; cancel also drops a sleep or shutdown that has not started yet."""
        if action == "wake":
            try:
                out = await asyncio.to_thread(argus.power.wake)
            except PowerError as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
            except OSError as e:
                raise HTTPException(status_code=502, detail=f"could not send the wake packet: {e}") from None
            await argus.store.write(lambda c: insert_event(c, time.time(), "power.wake_sent", src="helios",
                                                           dst="power", data=out))
            return out
        if action not in ("sleep", "shutdown", "restart", "cancel"):
            raise HTTPException(status_code=404, detail=f"no power action {action!r}")
        dropped = []
        if action == "cancel":
            await argus.power.hold()  # and no automatic shutdown for this idle stretch
            for j in await argus.jobs.list_jobs(None, 20, "power"):
                if j.state.value in ("queued", "retry") and j.workflow in ("sleep", "shutdown", "restart",
                                                                            "auto_shutdown"):
                    await argus.jobs.cancel(j.id)
                    dropped.append(j.id)
        needs = set(argus.cfg.power.pc_needs)
        if not any(w["state"] == "online" and set(w["capabilities"]) & needs for w in await argus.registry.workers()):
            if action == "cancel":  # the PC is off: nothing to cancel on it
                return {"id": None, "created": False, "dropped": dropped}
            raise HTTPException(status_code=409, detail="the PC is off")
        job_id, created = await argus.jobs.enqueue(
            "power", action, {"delay": argus.cfg.power.shutdown_delay_seconds}, needs=["desktop"], priority=100,
            dedupe_key=f"power:{action}", source="helios")
        return {"id": job_id, "created": created, "dropped": dropped}

    deps.laptop_power_action = laptop_power_action
    deps.power_action = power_action
    return router
