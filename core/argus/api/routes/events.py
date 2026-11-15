"""Events, the registry, the map and the /ws/events WebSocket (Helios)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ...events import EventFilter, read_events
from ._deps import Deps

WS_PING_SECONDS = 20.0
MAX_REPLAY = 10_000


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    token_ok = deps.token_ok

    @router.get("/events", dependencies=guarded)
    async def events(after: int = 0, limit: int = Query(200, ge=1, le=1000), kinds: str | None = None,
                     job: str | None = None, component: str | None = None, newest: bool = False,
                     wait: float = Query(0, ge=0, le=30)) -> dict:
        """Event history. `newest=true` returns the latest `limit` matches (oldest first). `wait=N`: when there is
        nothing new yet, wait up to N seconds for it (a long poll: the island and the listener ask once and hear at
        once, instead of asking every second all day)."""
        flt = EventFilter.parse(kinds, job, component)

        def read() -> Any:
            return argus.store.read(lambda c: read_events(c, after, limit=limit, flt=flt, newest=newest))

        if wait <= 0 or newest:
            return {"events": await read(), "seq": argus.hub.cursor}
        sub = argus.hub.subscribe(flt)  # before reading: nothing can slip in between
        try:
            rows = await read()
            if not rows:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(wait):
                        while True:
                            batch = await sub.queue.get()
                            if batch is None or any(int(e.get("seq") or 0) > after for e in batch):
                                break
                rows = await read()
        finally:
            argus.hub.unsubscribe(sub)
        return {"events": rows, "seq": argus.hub.cursor}

    @router.get("/registry", dependencies=guarded)
    async def registry() -> dict:
        return {"components": await argus.registry.components(), "workers": await argus.registry.workers()}

    @router.get("/map", dependencies=guarded)
    async def graph() -> dict:
        return await argus.registry.map()

    @router.websocket("/ws/events")
    async def ws_events(ws: WebSocket, since: int | None = None, kinds: str | None = None,
                        job: str | None = None, component: str | None = None, token: str | None = None):
        if not token_ok(ws.headers.get("authorization"), token):
            await ws.close(code=4401, reason="missing or wrong token")
            return
        await ws.accept()
        flt = EventFilter.parse(kinds, job, component)
        sub = argus.hub.subscribe(flt)
        try:
            await ws.send_json({"type": "hello", "version": argus.version, "seq": sub.after})
            if since is not None:
                cursor, sent = min(max(since, 0), sub.after), 0
                while cursor < sub.after:
                    rows = await argus.store.read(
                        lambda c, a=cursor: read_events(c, a, until=sub.after, limit=500, flt=flt))
                    if not rows:
                        break
                    await ws.send_json({"type": "events", "replay": True, "events": rows})
                    cursor, sent = rows[-1]["seq"], sent + len(rows)
                    if sent >= MAX_REPLAY and cursor < sub.after:
                        await ws.send_json({"type": "reset", "reason": "too far behind; reload the map"})
                        break
            # Wait for events and for the viewer leaving at the same time, so a closed tab (or Argus shutting
            # down) ends this handler at once instead of at the next ping.
            recv = asyncio.ensure_future(ws.receive())
            try:
                while True:
                    get = asyncio.ensure_future(sub.queue.get())
                    done, _ = await asyncio.wait({get, recv}, timeout=WS_PING_SECONDS,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if recv in done:
                        get.cancel()
                        if recv.result()["type"] == "websocket.disconnect":
                            return
                        recv = asyncio.ensure_future(ws.receive())  # viewers have nothing to say; ignore it
                        continue
                    if get not in done:
                        get.cancel()
                        await ws.send_json({"type": "ping", "seq": argus.hub.cursor})
                        continue
                    batch = get.result()
                    if batch is None:
                        if sub.dropped == "shutdown":
                            await ws.close(code=1001, reason="argus is stopping")
                        else:
                            await ws.close(code=4000, reason="fell behind; reconnect with since")
                        return
                    await ws.send_json({"type": "events", "events": batch})
            finally:
                recv.cancel()
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass  # the viewer went away
        finally:
            argus.hub.unsubscribe(sub)

    return router
