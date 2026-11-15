"""Ari's island: widgets and shortcuts."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from ... import ask as ask_mod
from ... import island as island_mod
from ...tools import ToolError
from ...weather import CODES as weather_codes
from ..schemas import AskDo
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    tools = deps.tools

    async def island_choices() -> list[dict]:
        acts = [{"action": a["id"], "label": a["label"], "group": "plugin buttons" if a["id"].startswith("run:")
                 else "power" if a["id"].startswith(("power:", "laptop:")) else "phone"}
                for a in ask_mod.catalog(argus.plugin_host) if not a["id"].startswith("show:")]
        acts += [{"action": f"show:{k}", "label": f"Open {v}", "group": "Helios pages"}
                 for k, v in island_mod.PAGES.items()]
        acts += [{"action": f"routine:{n}", "label": n, "group": "routines"}
                 for n in await argus.store.read(island_mod.routines)]
        return acts

    @router.get("/island", dependencies=guarded)
    async def island_get() -> dict:
        cfg = await argus.store.read(island_mod.load)
        return {**cfg, "widget_labels": island_mod.WIDGETS, "choices": await island_choices(),
                "max_shortcuts": island_mod.MAX_SHORTCUTS}

    @router.put("/island", dependencies=guarded)
    async def island_put(body: dict[str, Any]) -> dict:
        actions = {c["action"] for c in await island_choices()}
        if "keys" not in body:  # an older Helios: keep the keys you set
            body = {**body, "keys": (await argus.store.read(island_mod.load)).get("keys")}
        try:
            cfg = island_mod.check(body, actions)
        except island_mod.IslandError as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
        await argus.store.write(lambda c: island_mod.save(c, cfg))
        return await island_get()

    @router.get("/island/weather", dependencies=guarded)
    async def island_weather() -> dict:
        """Today's weather for the island's calendar page (brief.weather's town); {} when no town is set or the
        forecast can't be reached. Kept for an hour, like the brief's."""
        place = argus.cfg.brief.weather.strip()
        if not place:
            return {}
        try:
            w = await asyncio.wait_for(asyncio.to_thread(argus.weather.day, place, 0), 15)
        except Exception:  # noqa: BLE001 - no connection: the page goes without it
            return {}
        if not w:
            return {}
        code = w.get("code")
        return {"place": w.get("place"), "now": w.get("now"), "low": w.get("low"), "high": w.get("high"),
                "rain": w.get("rain"), "sky": weather_codes.get(int(code), "") if code is not None else ""}

    @router.post("/island/run", dependencies=guarded)
    async def island_run(body: AskDo) -> dict:
        """A shortcut on the island: a routine or a website here, everything else as in Ask."""
        a = body.action
        if a.startswith("url:"):
            if not re.fullmatch(r"url:https?://[^\s]{3,500}", a):
                raise HTTPException(status_code=422, detail="not a website")
            return {"open": a[4:]}
        if a.startswith("routine:"):
            try:
                tool = tools.get("run_routine")
                job_id, created = await tools.enqueue(tool, {"name": a[8:]}, parent=None, key=None)
            except ToolError as e:
                raise HTTPException(status_code=409, detail=str(e)) from None
            return {"job_id": job_id, "created": created}
        return await deps.do_action(a)

    return router
