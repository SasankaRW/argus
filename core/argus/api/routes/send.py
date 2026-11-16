"""Argus Send, Argus side: pairing (devices and their addresses) and the shared history. The agents work without it."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

from ._deps import Deps


class SendDevice(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9-]{1,32}$")
    name: str = Field(..., min_length=1, max_length=60)
    pubkey: str = Field(..., min_length=40, max_length=100)
    lan: list[str] = Field(default_factory=list, max_length=8)
    tailscale: str | None = Field(None, max_length=253)


class SendDeviceRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)


class SendEvent(BaseModel):
    eid: str = Field(..., min_length=8, max_length=64)
    device: str = Field(..., pattern=r"^[a-z0-9-]{1,32}$")
    kind: str = Field(..., max_length=30)
    at: float | None = None
    peer: str | None = Field(None, max_length=60)
    label: str | None = Field(None, max_length=300)
    bytes: int = Field(0, ge=0)
    detail: str | None = Field(None, max_length=500)
    offer_id: str | None = Field(None, max_length=64)


def build_router(deps: Deps) -> APIRouter:
    store = deps.argus.store
    router = APIRouter()
    guarded = deps.guarded

    @router.post("/send/devices", dependencies=guarded)
    async def register_device(body: SendDevice) -> dict:
        now = time.time()

        def w(conn):
            old = conn.execute("SELECT pubkey FROM send_device WHERE id=?", (body.id,)).fetchone()
            conn.execute(
                "INSERT INTO send_device(id,name,pubkey,lan,tailscale,seen_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET pubkey=excluded.pubkey, lan=excluded.lan, "
                "tailscale=excluded.tailscale, seen_at=excluded.seen_at",
                (body.id, body.name, body.pubkey, json.dumps(body.lan), body.tailscale, now))
            return old is not None and old[0] != body.pubkey

        return {"ok": True, "key_changed": await store.write(w)}

    @router.patch("/send/devices/{device_id}", dependencies=guarded)
    async def rename_device(body: SendDeviceRename, device_id: str = Path(..., pattern=r"^[a-z0-9-]{1,32}$")) -> dict:
        """Rename a device. The name is only set when a device first registers (the agents re-register every 30 s
        and must not undo this); every agent picks it up on its next sync and rewrites its devices.json."""
        name = body.name.strip()
        if not name:
            raise HTTPException(422, "the name is empty")

        def w(conn):
            return conn.execute("UPDATE send_device SET name=? WHERE id=?", (name, device_id)).rowcount

        if not await store.write(w):
            raise HTTPException(404, "no such device")
        return {"ok": True, "id": device_id, "name": name}

    @router.get("/send/devices", dependencies=guarded)
    async def list_devices() -> list[dict]:
        def r(conn):
            return [{"id": x["id"], "name": x["name"], "pubkey": x["pubkey"], "lan": json.loads(x["lan"]),
                     "tailscale": x["tailscale"], "seen_at": x["seen_at"]}
                    for x in conn.execute("SELECT * FROM send_device ORDER BY id")]

        return await store.read(r)

    @router.post("/send/events", dependencies=guarded)
    async def post_event(body: SendEvent) -> dict:
        at = body.at or time.time()

        def w(conn):
            if conn.execute("SELECT 1 FROM send_history WHERE eid=?", (body.eid,)).fetchone():
                return False
            conn.execute(
                "INSERT OR IGNORE INTO send_history(eid,at,device,kind,peer,label,bytes,detail,offer_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (body.eid, at, body.device, body.kind, body.peer, body.label, body.bytes, body.detail, body.offer_id))
            conn.execute("DELETE FROM send_history WHERE at < ?", (time.time() - 90 * 86400,))
            return True

        return {"ok": True, "stored": bool(await store.write(w))}

    @router.get("/send/history", dependencies=guarded)
    async def history(limit: int = Query(50, ge=1, le=200)) -> list[dict]:
        def r(conn):
            return [dict(x) for x in conn.execute("SELECT * FROM send_history ORDER BY at DESC LIMIT ?", (limit,))]

        return await store.read(r)

    return router
