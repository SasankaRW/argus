from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx
import uvicorn
from fastapi import FastAPI, Request

from _send_kit import free_port, make_agent, make_file, pair, wait_state
from argus.send.keys import DeviceKey, Ephemeral, request_mac, session_key, unb64, verify
from test_send_transfer import run_two


def test_keys_and_mac(tmp_path):
    k1, k2 = DeviceKey(tmp_path / "a.key"), DeviceKey(tmp_path / "b.key")
    assert DeviceKey(tmp_path / "a.key").pub == k1.pub  # stable across restarts
    sig = k1.sign(b"nonce")
    assert verify(k1.pub, b"nonce", sig) and not verify(k2.pub, b"nonce", sig) and not verify(k1.pub, b"x", sig)
    e1, e2 = Ephemeral(), Ephemeral()
    assert e1.shared(e2.pub) == e2.shared(e1.pub)
    a = session_key(e1.shared(e2.pub), b"c" * 32, b"s" * 32, k1.pub, k2.pub)
    assert a == session_key(e2.shared(e1.pub), b"c" * 32, b"s" * 32, k1.pub, k2.pub) and len(a) == 32
    assert request_mac(a, "PUT", "/p", "chunk=1", "ab", 1) != request_mac(a, "PUT", "/p", "chunk=2", "ab", 1)
    assert request_mac(a, "PUT", "/p", "", "ab", 1) != request_mac(a, "PUT", "/p", "", "ab", 2)
    assert len(unb64(k1.pub)) == 32


def test_unknown_key_403_and_bad_mac_401(tmp_path):
    src = tmp_path / "f.bin"
    make_file(src, 1000)

    async def body(a, b):
        base = f"http://127.0.0.1:{b.cfg.port}"
        async with httpx.AsyncClient() as c:
            # a device Calypso has never paired with
            r = await c.post(base + "/send/hello", json={"from": "stranger", "nonce_c": "AAAA"})
            assert r.status_code == 403
            # no session at all
            assert (await c.post(base + "/send/offers", json={})).status_code == 401
        # a bad MAC on a good session
        link = await a.sender.hello("calypso", "lan", base)
        bad = dataclasses.replace(link, key=b"k" * 32)
        r = await a.sender.call(bad, "GET", "/send/offers/00000000-aaaa", target="calypso", _retry=False)
        assert r.status_code == 401
        good = await a.sender.call(link, "GET", "/send/offers/00000000-aaaa", target="calypso")
        assert good.status_code == 404  # authenticated, and there is no such offer
        # a device that claims to be "pc" with another key: its requests are refused (403) and it is not retried
        c_agent = make_agent(tmp_path, "pc", "Imposter", data_dir=str(tmp_path / "imp"), root=str(tmp_path / "imp-r"))
        c_agent.devices.put(b.me())
        await c_agent.start()
        try:
            oid = c_agent.enqueue("calypso", [src])
            item = await wait_state(c_agent, oid, "failed")
            assert "not recognised" in item["reason"]
            assert not (b.root / "From PC").exists()
        finally:
            await c_agent.stop()

    run_two(tmp_path, body)


def test_argus_down_changes_nothing(tmp_path):
    src = tmp_path / "f.bin"
    data = make_file(src, 5 * 16 * 1024)
    dead = f"http://127.0.0.1:{free_port()}"

    async def go():
        a = make_agent(tmp_path, "pc", "PC", argus_url=dead)
        b = make_agent(tmp_path, "calypso", "Calypso")
        pair(a, b)
        known = json.dumps(a.devices.devices, sort_keys=True)
        await a.start()
        await b.start()
        try:
            await wait_state(a, a.enqueue("calypso", [src]), "done")
            assert (b.root / "From PC" / "f.bin").read_bytes() == data
            await a.argus_cycle()
            assert not a.argus_up
            assert json.dumps(a.devices.devices, sort_keys=True) == known  # devices.json untouched
            assert [e["kind"] for _i, e in a.store.outbox()] == ["sent"]  # the event waits for Argus
            assert a.store.history()[0]["kind"] == "sent"
        finally:
            await a.stop()
            await b.stop()

    asyncio.run(go())


def test_argus_sync_with_a_fake_argus(tmp_path):
    """Register, device list in, events out, and an outbox that empties once Argus answers."""
    seen = {"devices": [], "events": []}
    fake = FastAPI()

    @fake.post("/send/devices")
    async def reg(request: Request):
        seen["devices"].append(await request.json())
        return {"ok": True}

    @fake.get("/send/devices")
    async def lst():
        return [{"id": "phone", "name": "my phone", "pubkey": DeviceKey(tmp_path / "p.key").pub, "lan": [],
                 "tailscale": "phone.tail.example"}]

    @fake.post("/send/events")
    async def ev(request: Request):
        seen["events"].append(await request.json())
        return {"ok": True}

    port = free_port()

    async def go():
        srv = uvicorn.Server(uvicorn.Config(fake, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
        t = asyncio.create_task(srv.serve())
        while not srv.started:
            await asyncio.sleep(0.01)
        a = make_agent(tmp_path, "pc", "PC", argus_url=f"http://127.0.0.1:{port}")
        a.store.event("sent", peer="Calypso", label="x", nbytes=5)
        await a.argus_cycle()
        assert a.argus_up and a.devices.get("phone")["tailscale"] == "phone.tail.example"
        assert seen["devices"][0]["id"] == "pc" and seen["events"][0]["device"] == "pc" and not a.store.outbox()
        srv.should_exit = True
        await t
        a.store.close()

    asyncio.run(go())


def test_sniffed_traffic_cannot_be_replayed_or_forged(tmp_path):
    from _send_kit import Spy

    src = tmp_path / "s.bin"
    make_file(src, 20000)
    spy = Spy()

    async def body(a, b):
        await wait_state(a, a.enqueue("calypso", [src]), "done")
        offers = next(s for s in spy.sniffed if s[0] == "POST" and s[1].endswith("/send/offers"))
        hello = next(s for s in spy.sniffed if s[1].endswith("/send/hello"))
        async with httpx.AsyncClient() as c:
            # the same request again, byte for byte: its sequence number was used
            r = await c.post(offers[1], headers=offers[2], content=offers[3])
            assert r.status_code == 401
            # the sniffed session id with a made-up MAC and a fresh sequence number
            forged = {**offers[2], "x-send-seq": "9999", "x-send-mac": "00" * 32}
            assert (await c.post(offers[1], headers=forged, content=offers[3])).status_code == 401
            # the hello replayed: a new session appears, but without the sender's key nobody can prove it
            h = await c.post(hello[1], headers=hello[2], content=hello[3])
            assert h.status_code == 200 and h.json()["sid"] != offers[2]["x-send-session"]
            mine = {**offers[2], "x-send-session": h.json()["sid"], "x-send-seq": "1"}
            assert (await c.post(offers[1], headers=mine, content=offers[3])).status_code == 403
        assert not list((b.root / ".incoming").iterdir())

    run_two(tmp_path, body, a_kw={"transport": spy})
