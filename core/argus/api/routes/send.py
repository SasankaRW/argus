"""Argus Send, Argus side: pairing (devices and their addresses) and the shared history. The agents work without it."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ...events import insert_event
from ._deps import Deps


class SendDevice(BaseModel):
    id: str = Field(..., pattern=r"^[a-z0-9-]{1,32}$")
    name: str = Field(..., min_length=1, max_length=60)
    pubkey: str = Field(..., min_length=40, max_length=100)
    lan: list[str] = Field(default_factory=list, max_length=8)
    tailscale: str | None = Field(None, max_length=253)


class SendLiveIn(BaseModel):
    device: str = Field(..., pattern=r"^[a-z0-9-]{1,32}$")
    items: list[dict] = Field(default_factory=list, max_length=50)


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


LINK_SECONDS = 600  # an "Open" link from the Transfers page works this long
AGENT_PORT = 8610
OID = r"^[A-Za-z0-9-]{8,64}$"


def build_router(deps: Deps) -> APIRouter:
    store = deps.argus.store
    router = APIRouter()
    guarded = deps.guarded

    # ---- received files: Helios's "Open". The files stay on the device that received them; Argus asks that
    # device's Send agent (with the worker token) and passes them on. Links are signed and short-lived, so a browser
    # tab, a download or the phone's browser can open them without the token.
    def token() -> str:
        return deps.argus.cfg.secrets.worker_token or ""

    def sign(oid: str, n: int, exp: int) -> str:
        return hmac.new(token().encode(), f"send-file/{oid}/{n}/{exp}".encode(), hashlib.sha256).hexdigest()[:40]

    async def holder(oid: str) -> tuple[str, list[str]]:
        """The device that received this share, and where its agent listens."""
        def r(conn):
            row = conn.execute("SELECT device FROM send_history WHERE offer_id=? AND kind='received' "
                               "ORDER BY at DESC LIMIT 1", (oid,)).fetchone()
            if not row:
                return None, None
            return row["device"], conn.execute("SELECT lan, tailscale FROM send_device WHERE id=?",
                                               (row["device"],)).fetchone()

        device, dev = await store.read(r)
        if not device:
            raise HTTPException(404, "no device has received this")
        addrs = []
        if dev is not None:
            addrs = [a for a in json.loads(dev["lan"] or "[]") if a]
            if dev["tailscale"]:
                addrs.append(f"{dev['tailscale']}:{AGENT_PORT}")
        return device, addrs

    async def ask_agent(addrs: list[str], path: str, stream: bool = False):
        headers = {"Authorization": f"Bearer {token()}"}
        last: Exception | None = None
        for a in addrs:
            client = httpx.AsyncClient(timeout=httpx.Timeout(10, read=60))
            try:
                r = await client.send(client.build_request("GET", f"http://{a}{path}", headers=headers), stream=stream)
            except httpx.HTTPError as e:
                last = e
                await client.aclose()
                continue
            if not stream:
                await client.aclose()
            return r, client
        raise HTTPException(502, f"the device is out of reach ({last})" if last else "the device has no address")

    @router.get("/send/files/{offer_id}", dependencies=guarded)
    async def received_files(offer_id: str = Path(..., pattern=OID)) -> dict:
        """The files of a received share, each with a short-lived link that opens it."""
        device, addrs = await holder(offer_id)
        if device.startswith("phone"):  # the phone app keeps them in Downloads > Argus and doesn't serve files
            return {"device": device, "files": [], "reason": "on the phone"}
        r, _ = await ask_agent(addrs, f"/send/received/{offer_id}")
        if r.status_code == 404:
            return {"device": device, "files": [], "reason": "not kept"}  # an older agent, or the phone
        if r.status_code != 200:
            raise HTTPException(502, f"the device said {r.status_code}")
        exp = int(time.time()) + LINK_SECONDS
        files = [{**f, "url": f"/send/files/{offer_id}/{f['n']}?exp={exp}&sig={sign(offer_id, f['n'], exp)}"}
                 for f in r.json()]
        return {"device": device, "files": files}

    @router.get("/send/files/{offer_id}/{n}")
    async def received_file(n: int, exp: int, sig: str, offer_id: str = Path(..., pattern=OID)):
        """One received file, passed on from the device that has it (signed link: no token needed)."""
        if exp < time.time() or not hmac.compare_digest(sig, sign(offer_id, n, exp)):
            raise HTTPException(403, "this link has expired: open it again from Transfers")
        _, addrs = await holder(offer_id)
        r, client = await ask_agent(addrs, f"/send/received/{offer_id}/{n}", stream=True)
        if r.status_code != 200:
            await r.aclose()
            await client.aclose()
            raise HTTPException(404 if r.status_code == 404 else 502, "that file is no longer there")
        keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "content-length",
                                                                      "content-disposition", "last-modified")}

        async def close() -> None:
            await r.aclose()
            await client.aclose()

        return StreamingResponse(r.aiter_raw(), headers=keep, background=BackgroundTask(close))

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

    live = deps.argus.send_live

    @router.post("/send/live", dependencies=guarded)
    async def report_live(body: SendLiveIn) -> dict:
        """An agent's live transfers (kept in memory only); the reply names any it should cancel. A transfer that
        starts or finishes also leaves one small "send.live" event, so Helios and the island know to look."""
        now = time.time()
        cancel = live.report(body.device, body.items, now)
        changes = live.take_changes()
        if changes:
            await store.write(lambda c: [insert_event(c, now, "send.live", data=ch) for ch in changes])
        return {"cancel": cancel}

    @router.get("/send/live", dependencies=guarded)
    async def list_live() -> list[dict]:
        """Transfers moving now (and ones that finished in the last few seconds), kept in memory only."""
        return live.items(time.time())

    @router.post("/send/live/{offer_id}/cancel", dependencies=guarded)
    async def cancel_live(offer_id: str = Path(..., pattern=r"^[A-Za-z0-9-]{8,64}$")) -> dict:
        """Ask the sending device to stop; it hears on its next report (about half a second). A transfer no device
        reports on any more ends here at once."""
        now = time.time()
        ok = live.cancel(offer_id, now)
        changes = live.take_changes()
        if changes:
            await store.write(lambda c: [insert_event(c, now, "send.live", data=ch) for ch in changes])
        return {"ok": ok}

    @router.get("/send/history", dependencies=guarded)
    async def history(limit: int = Query(50, ge=1, le=200)) -> list[dict]:
        def r(conn):
            return [dict(x) for x in conn.execute("SELECT * FROM send_history ORDER BY at DESC LIMIT ?", (limit,))]

        return await store.read(r)

    return router
