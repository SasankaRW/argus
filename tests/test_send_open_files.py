"""Helios's "Open" on the Transfers page: the receiving agent keeps where it saved each share and hands the files to
Argus (worker token only); Argus lists them with short-lived signed links and passes each file on."""

from __future__ import annotations

import asyncio
import threading
import time

import uvicorn
from fastapi.testclient import TestClient

from _send_kit import free_port, make_agent
from argus.api import create_app
from argus.send.receiver import build_app
from test_send_api import KEY, make

OID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def received_agent(tmp_path):
    agent = make_agent(tmp_path, "pc", "PC")
    agent.argus_token = "t0ken"
    folder = tmp_path / "pc-root" / "From my phone" / "photos"
    folder.mkdir(parents=True)
    (folder / "a.jpg").write_bytes(b"jpeg-bytes")
    (folder / "b.txt").write_bytes(b"hello")
    agent.remember_received(OID, [str(folder / "a.jpg"), str(folder / "b.txt")])
    return agent


def test_the_agent_hands_received_files_to_argus_only(tmp_path):
    agent = received_agent(tmp_path)
    with TestClient(build_app(agent)) as c:
        assert c.get(f"/send/received/{OID}").status_code == 401  # no token
        assert c.get(f"/send/received/{OID}", headers={"Authorization": "Bearer nope"}).status_code == 401
        h = {"Authorization": "Bearer t0ken"}
        got = c.get(f"/send/received/{OID}", headers=h).json()
        want = [("photos/a.jpg", 10, True), ("photos/b.txt", 5, True)]
        assert [(f["name"], f["size"], f["here"]) for f in got] == want
        r = c.get(f"/send/received/{OID}/1", headers=h)
        assert r.content == b"hello" and "inline" in r.headers["content-disposition"]
        assert c.get(f"/send/received/{OID}/9", headers=h).status_code == 404
        assert c.get("/send/received/" + "a" * 36, headers=h).status_code == 404


def test_the_book_keeps_only_the_newest(tmp_path):
    agent = make_agent(tmp_path, "pc", "PC")
    agent.RECEIVED_KEEP = 3
    for i in range(5):
        agent.remember_received(f"offer-{i:04d}", [f"/x/{i}"])
    assert agent.received_files("offer-0001") is None and agent.received_files("offer-0004") == ["/x/4"]


def test_argus_lists_and_passes_files_on_with_signed_links(tmp_path):
    agent = received_agent(tmp_path)
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(build_app(agent), host="127.0.0.1", port=port, log_level="warning",
                                        lifespan="off"))
    t = threading.Thread(target=lambda: asyncio.run(srv.serve()), daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.01)
    try:
        (tmp_path / "argus").mkdir()
        with TestClient(create_app(make(tmp_path / "argus"))) as c:
            h = {"Authorization": "Bearer t0ken"}
            dev = {"id": "pc", "name": "PC", "pubkey": KEY, "lan": [f"127.0.0.1:{port}"], "tailscale": None}
            c.post("/send/devices", json=dev, headers=h)
            c.post("/send/events", headers=h, json={"eid": "rx" * 8, "device": "pc", "kind": "received",
                                                     "peer": "my phone", "label": "photos", "bytes": 15,
                                                     "offer_id": OID})
            assert c.get(f"/send/files/{OID}").status_code == 401
            listed = c.get(f"/send/files/{OID}", headers=h).json()
            assert listed["device"] == "pc" and [f["name"] for f in listed["files"]] == ["photos/a.jpg", "photos/b.txt"]
            url = listed["files"][0]["url"]
            r = c.get(url)  # no token: the signed link is enough
            assert r.status_code == 200 and r.content == b"jpeg-bytes"
            assert c.get(url.replace("sig=", "sig=0")).status_code == 403
            assert c.get(f"/send/files/{OID}/0?exp={int(time.time()) - 5}&sig=x").status_code == 403
            assert c.get("/send/files/" + "b" * 36, headers=h).status_code == 404  # nobody received it
            c.post("/send/events", headers=h, json={"eid": "ph" * 8, "device": "phone", "kind": "received",
                                                     "peer": "PC", "label": "a.apk", "bytes": 1, "offer_id": "c" * 36})
            assert c.get("/send/files/" + "c" * 36, headers=h).json() == {"device": "phone", "files": [],
                                                                           "reason": "on the phone"}
    finally:
        srv.should_exit = True
        t.join(5)
