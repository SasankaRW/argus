"""Ari's timeline: requests as stories, lanes, activity and problems."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ... import timeline as timeline_mod
from ...events import insert_event
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    @router.post("/ari/spoken", dependencies=guarded)
    async def ari_spoken(body: dict[str, Any]) -> dict:
        """The listener has Ari's answer: {conv, answer_ms (from when you finished speaking), spoken (or only shown)}
        on Ari's newest turn in that chat."""
        conv = str(body.get("conv") or "")
        add = {k: body.get(k) for k in ("answer_ms", "spoken", "empty") if k in body}

        def fn(c):
            r = c.execute("SELECT id, meta FROM ari_turns WHERE conv = ? AND role = 'ari' ORDER BY id DESC LIMIT 1",
                          (conv,)).fetchone()
            if r is None:
                return
            try:
                had = json.loads(r[1]) if r[1] else {}
            except ValueError:
                had = {}
            meta = {**(had if isinstance(had, dict) else {}), **add}  # the thread kept there stays
            c.execute("UPDATE ari_turns SET meta = ? WHERE id = ?", (json.dumps(meta), r[0]))
        await argus.store.write(fn)
        return {"ok": True}

    @router.post("/ari/ignored", dependencies=guarded)
    async def ari_ignored(body: dict[str, Any]) -> dict:
        """Speech the listener didn't take as a request, and why ("another language", "no wake phrase")."""
        data = {k: body.get(k) for k in ("why", "text", "lang", "top_p", "english") if body.get(k) is not None}
        data["text"] = str(data.get("text") or "")[:120]
        await argus.store.write(lambda c: insert_event(c, time.time(), "ari.ignored", src="pc", dst="ari",
                                                       data=data))
        return {"ok": True}

    @router.get("/ari/timeline", dependencies=guarded)
    async def ari_timeline(limit: int = Query(50, ge=1, le=300), before: float | None = None,
                           ignored: bool = True) -> dict:
        """Your requests to Ari as stories, newest first: heard, what Ari did, the answer, the result and why."""
        rows = await argus.store.read(lambda c: timeline_mod.requests(c, limit=limit, before=before,
                                                                     ignored=ignored))
        return {"requests": rows}

    @router.get("/ari/trace/{ref}", dependencies=guarded)
    async def ari_trace(ref: str) -> dict:
        """One request in full (its job id, its turn id, or "last"): every step, model call and tool, in order."""
        t = await argus.store.read(lambda c: timeline_mod.trace(c, ref))
        if t is None:
            raise HTTPException(status_code=404, detail="no such request")
        return t

    @router.get("/timeline/lanes", dependencies=guarded)
    async def timeline_lanes(minutes: float = Query(60, gt=0, le=24 * 60)) -> dict:
        """What each machine worked on in the last `minutes`: a lane of jobs per worker (the console's timeline)."""
        return await argus.store.read(lambda c: timeline_mod.lanes(c, time.time(), minutes))

    @router.get("/timeline/activity", dependencies=guarded)
    async def timeline_activity(hours: int = Query(24, gt=0, le=24 * 7)) -> dict:
        """Hour by hour: jobs finished (ok / failed) and requests to Ari (ok / failed / ignored), with totals."""
        return await argus.store.read(lambda c: timeline_mod.activity(c, time.time(), hours))

    @router.get("/problems", dependencies=guarded)
    async def problems(hours: float = Query(24, gt=0, le=24 * 14)) -> dict:
        """What went wrong lately, grouped by cause with counts, an example and a hint."""
        p = await argus.store.read(lambda c: timeline_mod.problems(c, time.time(), hours))
        return {**p, "headline": timeline_mod.headline(p)}

    return router
