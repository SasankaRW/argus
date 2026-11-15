"""Ask Argus: plain words in, an answer and at most one suggested action out; POST /ask/do runs it."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ... import ask as ask_mod
from ...config import PRIORITY_INTERACTIVE
from ..schemas import Ask, AskDo
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded

    @router.post("/ask", dependencies=guarded)
    async def ask(body: Ask) -> dict:
        """Plain words -> an answer and at most one suggested action (never done without POST /ask/do).
        Common asks are answered at once from data (rules); the rest go to a model as a job (poll its result)."""
        actions = ask_mod.catalog(argus.plugin_host)
        if ask_mod.PHONE.search(body.text.lower()):
            where = await deps.phone_where()
            return {"via": "rules", "reply": f"{where['text']} Ring it?", "action": "phone:ring",
                    "label": "Ring my phone"}
        snap = await ask_mod.snapshot(argus)
        hit = ask_mod.rules(body.text, actions, snap)
        labels = {a["id"]: a["label"] for a in actions}
        if hit is not None:
            return {"via": "rules", **hit, "label": labels.get(hit.get("action") or "")}
        job_id, _ = await argus.jobs.enqueue(
            "ask", "ask", {"text": body.text, "actions": actions, "snapshot": snap},
            needs=list(argus.cfg.models.needs), priority=PRIORITY_INTERACTIVE, source="helios")
        return {"via": "model", "job_id": job_id}

    @router.post("/ask/do", dependencies=guarded)
    async def ask_do(body: AskDo) -> dict:
        """Do a suggested action (you tapped it)."""
        return await do_action(body.action)

    async def do_action(a: str) -> dict:
        if a == "phone:ring":
            return {"phone": await deps.ring_phone()}
        if a.startswith("show:"):
            return {"view": a[5:]}
        if a.startswith("power:"):
            return {"power": await deps.power_action(a[6:])}
        if a in ("game:on", "game:off"):
            return {"game": await deps.set_game({"on": a == "game:on", "by": "you"})}
        if a.startswith("laptop:") and a != "laptop:shutdown":  # shutting the laptop down: Helios only, twice
            return {"power": await deps.laptop_power_action(a[7:], None)}
        if a.startswith("run:"):
            _, pid, wf = (a.split(":", 2) + ["", ""])[:3]
            p = argus.plugin_host.plugins.get(pid)
            if p is None or wf not in [t.manual.workflow for t in p.manifest.triggers if t.manual]:
                raise HTTPException(status_code=404, detail=f"no button {a!r}")
            job_id, created = await argus.jobs.enqueue(pid, wf, {}, needs=p.manifest.job_needs(),
                                                       priority=PRIORITY_INTERACTIVE, source="helios")
            return {"job_id": job_id, "created": created}
        raise HTTPException(status_code=404, detail=f"unknown action {a!r}")

    deps.do_action = do_action
    return router
