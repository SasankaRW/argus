"""Live activities (activities.py): what the island and the phone's Live notification show."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

from ._deps import Deps

CID = r"^[A-Za-z0-9_.-]{1,64}$"


class TimerIn(BaseModel):
    minutes: int = Field(..., ge=1, le=1440)
    label: str = Field("", max_length=80)


class CustomIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)
    detail: str = Field("", max_length=120)
    icon: str = Field("bolt", max_length=40, pattern=r"^[a-z0-9_]+$")
    progress: float | None = Field(None, ge=0, le=1)
    ends_at: float | None = None
    device: str | None = Field(None, max_length=32)


def build_router(deps: Deps) -> APIRouter:
    acts = deps.argus.activities
    router = APIRouter()
    guarded = deps.guarded  # the worker token or any live named token

    @router.get("/activities", dependencies=guarded)
    async def list_activities(after: int | None = None, wait: float = Query(0, ge=0, le=30)) -> dict:
        """{version, items}, most urgent first. `after=<version>&wait=25` sleeps until the version moves."""
        if after is None or wait <= 0:
            return acts.snapshot()
        return await acts.changed(after, wait)

    @router.post("/activities/timer", dependencies=guarded)
    async def start_timer(body: TimerIn) -> dict:
        """A timer on the PC (the island counts it down and chimes)."""
        return acts.start_timer(body.minutes, body.label)

    @router.put("/activities/custom/{cid}", dependencies=guarded)
    async def put_custom(body: CustomIn, cid: str = Path(..., pattern=CID)) -> dict:
        return acts.put_custom(cid, body.model_dump())

    @router.delete("/activities/custom/{cid}", dependencies=guarded)
    async def delete_custom(cid: str = Path(..., pattern=CID)) -> dict:
        if not acts.delete_custom(cid):
            raise HTTPException(404, "no such activity")
        return {"ok": True}

    return router
