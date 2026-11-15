"""Find my phone, the phone app's inbox and notifications, and the outbox."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from ...devices.presence import describe_phone
from ...events import insert_event
from ...outbox import add_message, phone_message
from ...tools import Tool
from ..schemas import InboxAck
from ._deps import Deps


def build_router(deps: Deps) -> APIRouter:
    argus = deps.argus
    router = APIRouter()
    guarded = deps.guarded
    tools = deps.tools

    async def ring_phone() -> dict:
        """Three urgent notifications, 20 s apart: the app plays them loud, even on silent."""
        if argus.outbox.inbox is None:
            raise HTTPException(status_code=409, detail="the phone app is off")

        def fn(conn):
            now = time.time()
            for i in range(3):
                add_message(conn, now, "phone", phone_message("Here I am!", "Argus is ringing your phone.",
                                                            priority="urgent", tags=["rotating_light", "iphone"]),
                            dedupe_key=f"ring:{int(now)}:{i}", send_at=now + 20 * i)
            insert_event(conn, now, "phone.ring", src="helios", dst="phone")

        await argus.store.write(fn)
        argus.outbox.poke()
        return {"ringing": True, "times": 3}

    async def phone_where() -> dict:
        if not argus.cfg.approvals.phone:
            return {"info": None, "text": "Set approvals.phone in argus.yaml (the phone's Tailscale name) and I can "
                                          "tell where it is."}
        try:
            info = await argus.phone.info()
        except Exception as e:  # Tailscale not running here
            return {"info": None, "text": f"I can't read Tailscale right now ({str(e)[:80]}).", }
        return {"info": info, "text": describe_phone(info)}

    async def _where(_: dict) -> dict:
        return await phone_where()

    async def _ring(_: dict) -> dict:
        return await ring_phone()

    tools.builtin["where_is_my_phone"] = Tool("where_is_my_phone", "Where the user's phone is (Tailscale: online at "
                                              "home, away, or offline and when last seen).", fn=_where)
    tools.builtin["ring_phone"] = Tool("ring_phone", "Ring the user's phone loudly to find it.", fn=_ring)

    @router.get("/phone", dependencies=guarded)
    async def get_phone() -> dict:
        """Where the phone is as far as Tailscale knows (online, home or away, last seen)."""
        return await phone_where()

    @router.post("/phone/ring", dependencies=guarded)
    async def post_phone_ring() -> dict:
        return await ring_phone()

    @router.post("/outbox/test", dependencies=guarded)
    async def outbox_test() -> dict:
        """Send a test notification, to check the phone app."""
        if argus.outbox.inbox is None:
            raise HTTPException(status_code=409, detail="the phone app is off")

        def fn(conn):
            return add_message(conn, time.time(), "phone", phone_message(
                "Argus test", f"The phone app works. Sent by {argus.cfg.instance.name} on {argus.cfg.instance.host}.",
                tags=["wave"]))

        oid = await argus.store.write(fn)
        argus.outbox.poke()
        return {"queued": oid}

    @router.get("/phone/inbox", dependencies=guarded)
    async def phone_inbox(device: str = "", wait: float = Query(0, ge=0, le=30), limit: int = Query(20, ge=1, le=50)
                          ) -> dict:
        """The phone app's messages it has not collected yet (a long poll: with `wait=N` it hears the moment one
        is queued). It shows them as notifications, then acknowledges them."""
        return {"messages": await argus.outbox.inbox_fetch(device, wait, limit)}

    @router.post("/phone/inbox/ack", dependencies=guarded)
    async def phone_inbox_ack(body: InboxAck) -> dict:
        return {"acked": await argus.outbox.inbox_ack(body.ids)}

    @router.get("/phone/notifications", dependencies=guarded)
    async def phone_notifications(limit: int = Query(50, ge=1, le=200)) -> dict:
        """The notifications sent to the phone, newest first (the Notifications page in Helios and the app)."""
        box = argus.outbox.inbox
        return {"notifications": await argus.outbox.notifications(limit),
                "phone": {"connected": bool(box and box.connected()), "last_seen": box.last_seen if box else 0.0,
                          "device": box.device if box else ""}}

    @router.get("/outbox", dependencies=guarded)
    async def outbox(limit: int = 50) -> dict:
        return {**(await argus.outbox.stats()), "messages": await argus.outbox.recent(min(max(limit, 1), 500)),
                **argus.outbox.health()}

    deps.phone_where = phone_where
    deps.ring_phone = ring_phone
    return router
