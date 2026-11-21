"""Ari's memory and chats: episodes, remembered facts, conversations and their export."""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from ... import ask as ask_mod
from ...ari import ari as ari_mod
from ...ari import ari_export as export_mod
from ...config import PRIORITY_BATCH
from ...events import EventFilter, read_events
from ...jobs import JobNotFound
from ...tools import Tool, ToolError
from ._deps import Deps

log = logging.getLogger("argus.api")


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    tools = deps.tools

    async def remember_in_background(conv: str) -> None:
        """Ari's memory over time, written by the local model after the answer (never in its way): this chat's
        turns that fell out of the window into its running summary, and chats that went quiet into episodes."""
        now = time.time()
        try:
            todo = await argus.store.read(lambda c: ari_mod.to_summarize(c, conv, now))
            if todo:
                await argus.jobs.enqueue("ari", "summarize", todo, needs=list(argus.cfg.models.needs),
                                         priority=PRIORITY_BATCH, dedupe_key=f"ari-summary:{conv}", max_attempts=1,
                                         delay=30, source="ari")
            for ep in await argus.store.read(lambda c: ari_mod.ended_chats(c, now, limit=2)):
                await argus.jobs.enqueue("ari", "episode", ep, needs=list(argus.cfg.models.needs),
                                         priority=PRIORITY_BATCH, dedupe_key=f"ari-episode:{ep['conv']}",
                                         max_attempts=1, delay=5, source="ari")
        except Exception as e:  # noqa: BLE001 - memory is a nicety: the answer goes on
            log.warning("ari memory not queued", extra={"error": str(e)[:200]})

    async def _keep_summary(x: dict) -> dict:
        await argus.store.write(lambda c: ari_mod.keep_summary(c, str(x["conv"]), str(x["summary"]),
                                                               int(x.get("upto") or 0)))
        return {"kept": True}

    async def _keep_episode(x: dict) -> dict:
        eid = await argus.store.write(lambda c: ari_mod.keep_episode(
            c, str(x["conv"]), str(x["episode"]), str(x.get("left_open") or ""), float(x.get("started") or 0),
            float(x.get("ended") or 0)))
        return {"episode": eid}

    tools.builtin["keep_summary"] = Tool(
        "keep_summary", "(Ari's memory) keep a chat's running summary",
        {"conv": {"type": "string"}, "summary": {"type": "string"}, "upto": {"type": "integer"}},
        ["conv", "summary", "upto"], fn=_keep_summary, for_ari=False, for_mcp=False)
    tools.builtin["keep_episode"] = Tool(
        "keep_episode", "(Ari's memory) keep what happened in a finished chat",
        {"conv": {"type": "string"}, "episode": {"type": "string"}, "left_open": {"type": "string"},
         "started": {"type": "number"}, "ended": {"type": "number"}},
        ["conv", "episode"], fn=_keep_episode, for_ari=False, for_mcp=False)

    @router.get("/ari-memory/episodes", dependencies=guarded)
    async def ari_episodes(limit: int = 50) -> list[dict]:
        """What happened in past chats (newest first), as Ari remembers them."""
        def read(c):
            rows = c.execute("SELECT id, conv, text, left_open, started_at, ended_at FROM ari_episodes"
                             " ORDER BY ended_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
            return [dict(r) for r in rows]
        return await argus.store.read(read)

    @router.delete("/ari-memory/episodes/{episode_id}", dependencies=guarded)
    async def ari_forget_episode(episode_id: int) -> dict:
        n = await argus.store.write(lambda c: c.execute("DELETE FROM ari_episodes WHERE id = ?",
                                                        (episode_id,)).rowcount)
        if not n:
            raise HTTPException(status_code=404, detail="no such episode")
        return {"ok": True}

    @router.get("/ari-memory", dependencies=guarded)
    async def ari_memory() -> list[dict]:
        """Everything you asked Ari to remember, newest first."""
        return await argus.store.read(ari_mod.memories)

    @router.delete("/ari-memory/{memory_id}", dependencies=guarded)
    async def ari_forget(memory_id: int) -> dict:
        if not await argus.store.write(lambda c: ari_mod.forget(c, memory_id)):
            raise HTTPException(status_code=404, detail="no such memory")
        return {"ok": True}

    async def _remember(x: dict) -> dict:
        mid = await argus.store.write(lambda c: ari_mod.remember(c, str(x["fact"])))
        return {"remembered": x["fact"], "id": mid}

    async def _recall(x: dict) -> list:
        return await argus.store.read(lambda c: ari_mod.recall(c, str(x["query"]), 10))

    async def _forget(x: dict) -> dict:
        ok = await argus.store.write(lambda c: ari_mod.forget(c, int(x["id"])))
        if not ok:
            raise ToolError(f"no memory {x['id']}")
        return {"forgotten": int(x["id"])}

    tools.builtin["remember"] = Tool("remember", "Keep a fact the user asked you to remember (one short sentence).",
                                     {"fact": {"type": "string", "description": "the fact, in the user's words"}},
                                     ["fact"], fn=_remember)
    tools.builtin["recall_memory"] = Tool("recall_memory", "Look up what the user asked you to remember earlier.",
                                          {"query": {"type": "string", "description": "words to look for"}},
                                          ["query"], fn=_recall)
    async def _search_memory(x: dict) -> dict:
        return await argus.store.read(lambda c: ari_mod.search_memory(c, str(x["query"]), time.time()))

    tools.builtin["search_my_memory"] = Tool(
        "search_my_memory", "Search what the user asked you to remember AND what was said in past conversations "
        "with Ari (\"what did I ask you about the router last week?\", \"what was that film I mentioned?\").",
        {"query": {"type": "string", "description": "words to look for"}}, ["query"], fn=_search_memory)
    tools.builtin["forget_memory"] = Tool("forget_memory", "Forget one remembered fact (its id from recall_memory).",
                                          {"id": {"type": "integer", "description": "the memory's id"}}, ["id"],
                                          fn=_forget)

    @router.get("/ari-chats", dependencies=guarded)
    async def ari_chats() -> list[dict]:
        """Ari's past conversations, newest first (for the chat list in Helios)."""
        return await argus.store.read(lambda c: ari_mod.chats(c))

    @router.delete("/ari-chats/{conv}", dependencies=guarded)
    async def ari_delete_chat(conv: str) -> dict:
        n = await argus.store.write(lambda c: ari_mod.delete_chat(c, conv))
        if not n:
            raise HTTPException(status_code=404, detail="no such chat")
        return {"deleted": n}

    @router.get("/ari/{conv}/export", dependencies=guarded)
    async def ari_export(conv: str, last: int = Query(40, ge=1, le=500)) -> Response:
        """The conversation and everything around it as one text file (what Ari did, errors, events, logs, settings):
        what you hand over when something went wrong. Secrets are removed."""

        def fn(conn):
            ev = read_events(conn, 0, limit=1, flt=EventFilter.parse("ari.health"), newest=True)
            return export_mod.build(conn, conv, cfg=argus.cfg, version=argus.version, log_dir=argus.cfg.log_dir,
                                    health=ev[-1]["data"] if ev else None, last=last,
                                    private={t.name for t in tools.all().values() if t.private})

        text = await argus.store.read(fn)
        if text is None:
            raise HTTPException(status_code=404, detail="no such chat")
        name = f"ari-{conv[:12]}-{time.strftime('%Y%m%d-%H%M%S')}.md"
        return Response(text, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @router.get("/ari/{conv}", dependencies=guarded)
    async def ari_conv(conv: str) -> dict:
        """The conversation; a model's answer is filled in here once its job finished."""
        rows = await argus.store.read(lambda c: ari_mod.turns(c, conv))
        dropped = []
        for t in rows:
            if t["job_id"] and t["text"] is None:
                try:
                    j = await argus.jobs.get(t["job_id"])
                except JobNotFound:
                    j = None
                if j is not None and j.state.value == "cancelled":  # you went on talking: no answer, no turn
                    dropped.append(t)
                    await argus.store.write(lambda c, t=t: c.execute("DELETE FROM ari_turns WHERE id = ?", (t["id"],)))
                    continue
                if j is None or j.state.value == "dead":
                    t["text"] = "Sorry, I couldn't answer that just now."
                elif j.state.value == "succeeded":
                    r = j.result or {}
                    t["text"] = str(r.get("reply") or "…")
                    t["used"] = [{"tool": u.get("tool"), "ok": "error" not in u} for u in r.get("used") or []] + \
                        ([{"tool": "web search", "ok": True}] if r.get("via") == "web" else [])
                    t["action"] = r.get("action")
                    if r.get("pending"):  # a tool that asks first
                        t["pending"] = r["pending"]
                    elif t["action"] and not str(t["action"]).startswith("show:"):
                        t["pending"] = {"kind": "action", "action": t["action"]}
                else:
                    continue
                await argus.store.write(lambda c, t=t: c.execute(
                    "UPDATE ari_turns SET text = ?, action = ?, pending = ?, used = ? WHERE id = ?",
                    (t["text"], t["action"], json.dumps(t["pending"]) if t["pending"] else None,
                     json.dumps(t.get("used")) if t.get("used") else None, t["id"])))
        rows = [t for t in rows if t not in dropped]
        labels = {a["id"]: a["label"] for a in ask_mod.catalog(argus.plugin_host)}
        for t in rows:
            t["label"] = labels.get(t["action"] or "")
        return {"conv": conv, "turns": rows}

    deps.remember_in_background = remember_in_background
    return router
