"""Agents report live progress to Argus while files move, and stop when a share is cancelled (from Argus or the CLI)."""

from __future__ import annotations

import asyncio

import uvicorn
from fastapi import FastAPI, Request

from _send_kit import Spy, free_port, make_agent, make_file, pair, wait_for, wait_state
from argus.send import cli

CH = 16 * 1024


def fake_argus(cancel_when=None):
    """A stand-in Argus: takes registrations and events, records /send/live posts, and can ask for a cancel."""
    seen: dict = {"live": [], "asked": False}
    app = FastAPI()

    @app.post("/send/devices")
    async def reg(request: Request):
        return {"ok": True}

    @app.get("/send/devices")
    async def lst():
        return []

    @app.post("/send/events")
    async def ev(request: Request):
        return {"ok": True}

    @app.post("/send/live")
    async def live(request: Request):
        body = await request.json()
        seen["live"].append(body)
        if cancel_when and not seen["asked"]:
            hit = [i["offer_id"] for i in body["items"] if body["device"] == "pc" and cancel_when(i)]
            if hit:
                seen["asked"] = True
                return {"cancel": hit}
        return {"cancel": []}

    return app, seen


async def serve(app, port):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.01)
    return srv, task


def run(tmp_path, body, *, cancel_when=None, delay=0.0, argus=True):
    async def go():
        app, seen = fake_argus(cancel_when)
        port = free_port()
        srv, task = await serve(app, port)
        url = f"http://127.0.0.1:{port}" if argus else f"http://127.0.0.1:{free_port()}"  # closed port: Argus down
        spy = Spy()
        spy.delay = delay
        a = make_agent(tmp_path, "pc", "PC", transport=spy, argus_url=url)
        b = make_agent(tmp_path, "phone", "my phone", argus_url=url)
        pair(a, b)
        await a.start()
        await b.start()
        try:
            return await body(a, b, seen)
        finally:
            await a.stop()
            await b.stop()
            srv.should_exit = True
            await task

    return asyncio.run(go())


def legs(seen, device):
    return [i for post in seen["live"] if post["device"] == device for i in post["items"]]


def test_a_transfer_reports_progress_then_done(tmp_path):
    src = tmp_path / "src" / "big.bin"
    make_file(src, 160 * CH)

    async def body(a, b, seen):
        oid = a.enqueue("phone", [src])
        await wait_state(a, oid, "done")
        await wait_for(lambda: any(i["state"] == "done" for i in legs(seen, "pc")), 10, "a final report")
        mine = [i for i in legs(seen, "pc") if i["offer_id"] == oid]
        assert mine[0]["state"] == "moving" and mine[0]["to"] == "phone" and mine[0]["total"] == 160 * CH
        assert any(i["state"] == "moving" and 0 < i["done"] < i["total"] for i in mine)  # it moved in between
        assert mine[-1]["state"] == "done" and mine[-1]["done"] == 160 * CH
        assert any(i["from"] == "pc" and i["to"] == "phone" for i in legs(seen, "phone"))  # the receiver reports too
        await wait_for(lambda: not a.live and not b.live, 10, "both ends to go quiet")
        n = len(seen["live"])
        await asyncio.sleep(1.5)
        assert len(seen["live"]) == n  # no reports once nothing moves

    run(tmp_path, body, delay=0.04)


def test_cancel_from_argus_stops_the_sender(tmp_path):
    src = tmp_path / "src" / "big.bin"
    make_file(src, 200 * CH)

    async def body(a, b, seen):
        oid = a.enqueue("phone", [src])
        await wait_state(a, oid, "cancelled")
        assert [e["kind"] for e in a.store.history(5)][0] == "cancelled"
        await wait_for(lambda: any(i["state"] == "cancelled" for i in legs(seen, "pc")), 10, "the cancelled report")
        assert not (tmp_path / "phone-root" / "From PC" / "big.bin").exists()

    run(tmp_path, body, cancel_when=lambda i: i["done"] > 0, delay=0.04)


def test_argus_down_doesnt_stop_a_send(tmp_path):
    src = tmp_path / "src" / "a.bin"
    make_file(src, 10 * CH)

    async def body(a, b, seen):
        oid = a.enqueue("phone", [src])
        await wait_state(a, oid, "done")

    run(tmp_path, body, argus=False)


def test_cli_cancel_marks_the_item(tmp_path, capsys):
    cfg = tmp_path / "argus.yaml"
    data = tmp_path / "send-data"
    cfg.write_text("send:\n  enabled: true\n  device_id: pc\n  name: PC\n"
                   f"  data_dir: {data.as_posix()}\n  root: {(tmp_path / 'shared').as_posix()}\n  mdns: false\n",
                   encoding="utf-8")
    src = tmp_path / "a.txt"
    src.write_text("hi", encoding="utf-8")
    a = make_agent(tmp_path, "phone", "my phone")
    import json
    data.mkdir(parents=True, exist_ok=True)
    (data / "devices.json").write_text(json.dumps([a.me()]), encoding="utf-8")
    a.store.close()
    assert cli.main(["--config", str(cfg), "send", "phone", str(src)]) == 0
    oid_short = capsys.readouterr().out.split()[1].rstrip(":")
    from argus.send.store import QueueStore
    q = QueueStore(data / "send-queue.db")
    oid = next(i["id"] for i in q.all() if i["id"].startswith(oid_short))
    q.close()
    assert cli.main(["--config", str(cfg), "cancel-url", f"argus-send://cancel/{oid}"]) == 0
    q = QueueStore(data / "send-queue.db")
    assert q.get(oid)["state"] == "cancelled"
    q.close()
    assert cli.main(["--config", str(cfg), "send", "cancel", oid]) == 1  # nothing left to cancel


def test_a_transfer_finishing_during_a_report_is_reported_done(tmp_path):
    """A tiny share (a small folder) can finish while its first "moving" report is still on the way: its "done"
    must still reach Argus, or Helios shows it stuck at 0%."""
    agent = make_agent(tmp_path, "pc", "PC")
    posts: list[list[dict]] = []

    async def slow_post(items):
        posts.append(items)
        if len(posts) == 1:  # the transfer finishes while the first report is in flight
            agent.progress("f" * 8, "send", src="pc", dst="calypso", label="folder", files=2, total=4, done=4,
                           state="done")
        await asyncio.sleep(0)
        return []

    async def go():
        agent._loop = asyncio.get_running_loop()
        agent._post_live = slow_post
        loop_task = asyncio.create_task(agent._live_loop())
        agent.progress("f" * 8, "send", src="pc", dst="calypso", label="folder", files=2, total=4, done=0)
        await wait_for(lambda: not agent.live and posts, 5, "the live loop to empty")
        loop_task.cancel()

    asyncio.run(go())
    assert posts[-1][0]["state"] == "done"
