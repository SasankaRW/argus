from __future__ import annotations

import asyncio
import time

from _send_kit import free_port, make_agent, make_file, pair, tree, wait_for, wait_state


def test_holding_area_delivers_later_and_is_dropped_when_sent_directly(tmp_path):
    src = tmp_path / "doc.bin"
    data = make_file(src, 3 * 16 * 1024)

    async def go():
        phone_port = free_port()
        a = make_agent(tmp_path, "pc", "PC", holder="calypso")
        hold = make_agent(tmp_path, "calypso", "Calypso", holding=True)
        phone = make_agent(tmp_path, "phone", "my phone", port=phone_port)
        pair(a, hold)
        pair(a, phone)
        pair(hold, phone)
        await a.start()
        await hold.start()
        try:
            oid = a.enqueue("phone", [src])  # the phone is off
            item = await wait_state(a, oid, "held")
            assert item["holder"] == "calypso"
            held = hold.root / ".holding" / oid
            assert (held / "0.data").exists() and hold.store.get(oid)["kind"] == "hold"
            assert not (phone.root / "From PC").exists()
            # the phone wakes: delivered, from the holding area or straight from the PC, whichever gets there first
            await phone.start()
            try:
                await wait_for(lambda: (phone.root / "From PC" / "doc.bin").exists()
                               and not held.exists(), what="delivery and a clean holding area")
                assert tree(phone.root / "From PC") == {"doc.bin": data}  # once, not twice
                assert phone.store.history()[0]["kind"] == "received"
                assert hold.store.get(oid)["state"] in ("done", "cancelled")
            finally:
                await phone.stop()
        finally:
            await a.stop()
            await hold.stop()

    asyncio.run(go())


def test_holding_area_refuses_when_not_a_holder_and_expires_after_days(tmp_path):
    src = tmp_path / "x.bin"
    make_file(src, 1000)

    async def go():
        a = make_agent(tmp_path, "pc", "PC", holder="calypso")
        hold = make_agent(tmp_path, "calypso", "Calypso", holding=False)
        phone = make_agent(tmp_path, "phone", "my phone")
        pair(a, hold)
        pair(a, phone)
        await a.start()
        await hold.start()
        try:
            oid = a.enqueue("phone", [src])
            item = await wait_for(lambda: (i := a.store.get(oid))["state"] == "failed" and i, what="a refusal")
            assert "does not hold" in item["reason"]
            # now make it a holder and let a share sit for 8 days
            hold.cfg.holding = True
            oid2 = a.enqueue("phone", [src])
            await wait_state(a, oid2, "held")
            assert hold.sweep_holding() == []
            assert hold.sweep_holding(now=time.time() + 8 * 86400) == [oid2]
            assert not (hold.root / ".holding" / oid2).exists()
            assert hold.store.get(oid2)["state"] == "failed"
            assert "not_delivered" in [h["kind"] for h in hold.store.history()]
        finally:
            await a.stop()
            await hold.stop()

    asyncio.run(go())


def test_held_share_reaches_done_on_the_sender(tmp_path):
    src = tmp_path / "w.bin"
    data = make_file(src, 2 * 16 * 1024)

    async def go():
        a = make_agent(tmp_path, "pc", "PC", holder="calypso")
        hold = make_agent(tmp_path, "calypso", "Calypso", holding=True)
        phone = make_agent(tmp_path, "phone", "my phone")
        pair(hold, phone)
        pair(a, hold)
        # the PC can only reach the phone through Calypso (its own address for the phone is dead)
        a.devices.put({**phone.me(), "lan": [f"127.0.0.1:{free_port()}"]})
        phone.devices.put(a.me())
        await a.start()
        await hold.start()
        try:
            oid = a.enqueue("phone", [src])
            await wait_state(a, oid, "held")
            await phone.start()
            try:
                await wait_state(a, oid, "done")  # Calypso told the PC
                assert tree(phone.root / "From PC") == {"w.bin": data}
                assert not (hold.root / ".holding" / oid).exists()
            finally:
                await phone.stop()
        finally:
            await a.stop()
            await hold.stop()

    asyncio.run(go())


def test_held_share_done_via_argus_event_and_tailscale_parse(tmp_path):
    import uvicorn
    from fastapi import FastAPI

    from argus.send.agent import parse_online

    assert parse_online({"Peer": {"k1": {"HostName": "Phone", "Online": True}, "k2": {"HostName": "pc"}}}) == {"phone"}
    port = free_port()
    oid_fixed = "11111111-2222"
    fake = FastAPI()
    fake.post("/send/devices")(lambda: {"ok": True})
    fake.get("/send/devices")(lambda: [])
    fake.post("/send/events")(lambda: {"ok": True})
    fake.get("/send/history")(lambda: [{"kind": "delivered", "device": "calypso", "offer_id": oid_fixed}])
    src = tmp_path / "v.bin"
    make_file(src, 100)

    async def go():
        srv = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
        t = asyncio.create_task(srv.serve())
        await wait_for(lambda: srv.started, what="fake Argus")
        a = make_agent(tmp_path, "pc", "PC", argus_url=f"http://127.0.0.1:{port}")
        a.devices.put(make_agent(tmp_path, "calypso", "Calypso").me())
        a.devices.put(make_agent(tmp_path, "phone", "my phone").me())
        oid = a.enqueue("phone", [src])
        a.store.set(oid, state="held", holder="calypso")
        a.store._x("UPDATE queue SET id=? WHERE id=?", (oid_fixed, oid))
        await a.argus_cycle()
        assert a.store.get(oid_fixed)["state"] == "done"
        srv.should_exit = True
        await t
        a.store.close()

    asyncio.run(go())
