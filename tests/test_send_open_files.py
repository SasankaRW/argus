"""Helios's "Open" on the Transfers page: each device keeps what it sent and received and where the files are; Helios
on that same device asks it directly (never through Argus, so it works while Argus is down) to list them and to open
one or show it in its folder. Worker token required, and only Helios's own address may ask from a page."""

from __future__ import annotations

from fastapi.testclient import TestClient

from _send_kit import make_agent
from argus.send import receiver
from argus.send.receiver import build_app

OID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
SID = "1a1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
HELIOS = "https://calypso.example.ts.net"
H = {"Authorization": "Bearer t0ken"}


def device(tmp_path):
    agent = make_agent(tmp_path, "pc", "PC", argus_url=HELIOS)
    agent.argus_token = "t0ken"
    folder = tmp_path / "pc-root" / "From my phone" / "photos"
    folder.mkdir(parents=True)
    (folder / "a.jpg").write_bytes(b"jpeg-bytes")
    (folder / "b.txt").write_bytes(b"hello")
    agent.remember("received", OID, "photos", "my phone", [str(folder / "a.jpg"), str(folder / "b.txt")])
    mine = tmp_path / "Documents" / "report.pdf"
    mine.parent.mkdir()
    mine.write_bytes(b"%PDF")
    agent.remember("sent", SID, "report.pdf", "Calypso", [str(mine), str(tmp_path / "gone.txt")])
    return agent


def test_lists_this_devices_sent_and_received_files(tmp_path):
    with TestClient(build_app(device(tmp_path))) as c:
        assert c.get("/send/mine").status_code == 401
        got = c.get("/send/mine", headers=H).json()
        assert got["device"] == "pc"
        sent, rec = got["items"]  # newest first
        assert (sent["kind"], sent["label"], sent["peer"]) == ("sent", "report.pdf", "Calypso")
        assert [(f["name"], f["here"]) for f in sent["files"]] == [("report.pdf", True), ("gone.txt", False)]
        assert [(f["name"], f["size"]) for f in rec["files"]] == [("photos/a.jpg", 10), ("photos/b.txt", 5)]


def test_opens_and_reveals_on_this_device(tmp_path, monkeypatch):
    opened: list = []
    monkeypatch.setattr(receiver, "open_on_this_device", lambda p, reveal: opened.append((p.name, reveal)))
    with TestClient(build_app(device(tmp_path))) as c:
        assert c.post(f"/send/mine/received/{OID}/1/open").status_code == 401
        assert c.post(f"/send/mine/received/{OID}/1/open", headers=H).json() == {"ok": True}
        assert c.post(f"/send/mine/received/{OID}/0/reveal", headers=H).json() == {"ok": True}
        assert c.post(f"/send/mine/sent/{SID}/1/open", headers=H).status_code == 410  # moved or deleted
        assert c.post(f"/send/mine/sent/{OID}/0/open", headers=H).status_code == 404  # not something it sent
        assert c.post(f"/send/mine/other/{OID}/0/open", headers=H).status_code == 404
    assert opened == [("b.txt", False), ("a.jpg", True)]


def test_only_helios_may_ask_from_a_page(tmp_path):
    with TestClient(build_app(device(tmp_path))) as c:
        pre = {"Origin": HELIOS, "Access-Control-Request-Method": "GET",
               "Access-Control-Request-Private-Network": "true"}
        r = c.options("/send/mine", headers=pre)
        assert r.status_code == 204 and r.headers["access-control-allow-origin"] == HELIOS
        assert r.headers["access-control-allow-private-network"] == "true"
        assert c.options("/send/mine", headers={**pre, "Origin": "https://evil.example"}).status_code == 403
        assert c.get("/send/mine", headers={**H, "Origin": "https://evil.example"}).status_code == 403
        r = c.get("/send/mine", headers={**H, "Origin": HELIOS})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == HELIOS


def test_the_book_keeps_only_the_newest(tmp_path):
    agent = make_agent(tmp_path, "pc", "PC")
    agent.MINE_KEEP = 3
    for i in range(5):
        agent.remember("sent", f"offer-{i:04d}", "x", "PC", [f"/x/{i}"])
    assert agent.mine_files("sent", "offer-0001") is None and agent.mine_files("sent", "offer-0004") == ["/x/4"]


def test_a_finished_send_and_receive_are_remembered(tmp_path):
    """Through a real transfer: the sender remembers what it sent, the receiver where it saved it."""
    import asyncio

    from _send_kit import make_file, pair, wait_state

    a, b = make_agent(tmp_path, "pc", "PC"), make_agent(tmp_path, "calypso", "Calypso")
    pair(a, b)
    src = tmp_path / "out" / "notes.txt"
    make_file(src, 5000)

    async def go():
        await a.start()
        await b.start()
        try:
            oid = a.enqueue("calypso", [src])
            await wait_state(a, oid, "done")
            return oid
        finally:
            await a.stop()
            await b.stop()

    oid = asyncio.run(go())
    assert a.mine_files("sent", oid) == [str(src)]
    got = b.mine_files("received", oid)
    assert got and got[0].endswith("notes.txt")


def test_two_shares_in_the_same_clock_tick_still_list_newest_first(tmp_path, monkeypatch):
    # Windows clocks can give two quick saves the same time: the one saved later is still the newer one
    from argus.send import agent as agent_mod
    monkeypatch.setattr(agent_mod.time, "time", lambda: 1_800_000_000.0)
    a = make_agent(tmp_path, "pc", "PC", argus_url=HELIOS)
    a.remember("received", OID, "photos", "my phone", [])
    a.remember("sent", SID, "report.pdf", "Calypso", [])
    assert [e["kind"] for e in a.mine()] == ["sent", "received"]
