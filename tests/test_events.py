"""C5: events, the edge map, the live stream and the tail tool."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from contextlib import redirect_stdout

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from argus import ids
from argus.api import create_app
from argus.events import EventFilter, EventHub, insert_event, last_seq, prune_events, read_events
from argus.registry import Registry
from conftest import run
from test_worker import Server, client, make


def write(store, fn):
    return store.write_sync(fn)


# ------------------------------------------------------------------ storage


def test_edges_grow_and_announce_themselves_once(store):
    for _ in range(3):
        write(store, lambda c: insert_event(c, 1.0, "job.queued", src="argus", dst="demo"))
    write(store, lambda c: insert_event(c, 2.0, "note", src="argus"))  # no receiver: no edge
    edges = store.read_sync(lambda c: [dict(r) for r in c.execute("SELECT * FROM edges")])
    assert len(edges) == 1 and edges[0]["count"] == 3 and edges[0]["last_kind"] == "job.queued"
    kinds = [e["kind"] for e in store.read_sync(lambda c: read_events(c, 0))]
    assert kinds.count("edge.added") == 1


def test_seq_follows_commit_order_and_filters(store):
    write(store, lambda c: insert_event(c, 1.0, "job.queued", job_id="A"))
    write(store, lambda c: insert_event(c, 1.0, "worker.online", src="w"))
    write(store, lambda c: insert_event(c, 1.0, "job.running", job_id="B"))
    write(store, lambda c: insert_event(c, 1.0, "jobXqueued", job_id="A"))  # "_" must not act as a wildcard
    evs = store.read_sync(lambda c: read_events(c, 0))
    assert [e["seq"] for e in evs] == sorted(e["seq"] for e in evs)
    only_jobs = store.read_sync(lambda c: read_events(c, 0, flt=EventFilter.parse("job.")))
    assert [e["kind"] for e in only_jobs] == ["job.queued", "job.running"]
    a = store.read_sync(lambda c: read_events(c, 0, flt=EventFilter.parse(None, "A")))
    assert {e["job_id"] for e in a} == {"A"} and len(a) == 2
    under = store.read_sync(lambda c: read_events(c, 0, flt=EventFilter.parse("job_")))
    assert under == []
    assert store.read_sync(lambda c: read_events(c, evs[1]["seq"], until=evs[2]["seq"]))[0]["kind"] == "job.running"


def test_prune_keeps_newest_so_seq_never_goes_back(store):
    for i in range(5):
        write(store, lambda c, i=i: insert_event(c, float(i), "old"))
    top = store.read_sync(last_seq)
    assert write(store, lambda c: prune_events(c, older_than=1e9)) == 4  # all old, but the newest stays
    assert store.read_sync(last_seq) == top
    write(store, lambda c: insert_event(c, 9.0, "new"))
    assert store.read_sync(last_seq) == top + 1


def test_ids_stay_ordered_when_clock_steps_back(monkeypatch):
    t = [2_000_000.0]
    monkeypatch.setattr(ids.time, "time", lambda: t[0])
    a = ids.new_id()
    t[0] -= 5  # NTP pulls the clock back
    b = ids.new_id()
    t[0] += 10
    c = ids.new_id()
    assert a < b < c


# ------------------------------------------------------------------ hub


def test_hub_delivers_batches_filters_and_drops_slow_viewers(store):
    async def scenario():
        hub = EventHub(store, batch_window=0, poll_interval=0.05, queue_size=2)
        await hub.start()
        everything = hub.subscribe()
        jobs_only = hub.subscribe(EventFilter.parse("job."))
        slow = hub.subscribe()
        await store.write(lambda c: insert_event(c, 1.0, "job.queued", job_id="J"))
        await store.write(lambda c: insert_event(c, 1.0, "worker.online", src="w"))
        got = []
        while len(got) < 2:
            got += await asyncio.wait_for(everything.queue.get(), 2)
        assert [e["kind"] for e in got] == ["job.queued", "worker.online"]
        assert [e["kind"] for e in await asyncio.wait_for(jobs_only.queue.get(), 2)] == ["job.queued"]
        for i in range(5):  # nobody reads `slow`: its queue overflows and the hub lets it go
            await store.write(lambda c, i=i: insert_event(c, 1.0, f"x.{i}"))
            await asyncio.sleep(0.08)
        assert slow.dropped == "slow" and slow not in hub.subscribers
        assert hub.stats()["dropped_subscribers"] >= 1
        late = hub.subscribe()
        await hub.stop()
        assert late.dropped == "shutdown" and await late.queue.get() is None

    run(scenario())


def test_hub_wakes_on_commit_fast(store):
    async def scenario():
        hub = EventHub(store, batch_window=0.02, poll_interval=30)  # the poll can't be what delivers it
        await hub.start()
        sub = hub.subscribe()
        t0 = time.perf_counter()
        await store.write(lambda c: insert_event(c, 1.0, "ping"))
        await asyncio.wait_for(sub.queue.get(), 2)
        elapsed = time.perf_counter() - t0
        await hub.stop()
        return elapsed

    assert run(scenario()) < 0.5


# ------------------------------------------------------------------ registry and map


def test_worker_online_offline_events_and_map(store, clock):
    reg = Registry(store, clock=clock)

    async def scenario():
        await reg.component("argus", "core", "Argus", "pc")
        await reg.register_worker("w1", "pc", ["gpu"])
        clock.advance(100)
        assert await reg.mark_stale_workers(60) == ["w1"]
        await reg.touch_worker("w1")  # back again
        await reg.touch_worker("ghost")  # unknown: ignored
        return await reg.map()

    m = run(scenario())
    kinds = [e["kind"] for e in store.read_sync(lambda c: read_events(c, 0))]
    assert kinds.count("worker.online") == 2 and kinds.count("worker.offline") == 1
    nodes = {n["id"]: n for n in m["nodes"]}
    assert nodes["w1"]["state"] == "online" and nodes["argus"]["kind"] == "core"
    assert m["seq"] == store.read_sync(last_seq)


def test_jobs_add_plugin_boxes_and_lines(jobs, store):
    async def scenario():
        job_id, _ = await jobs.enqueue("demo", "echo")
        job = await jobs.claim("w1", [])
        await jobs.start(job.id, "w1")
        await jobs.succeed(job.id, "w1", {"ok": True})
        return await Registry(store).map()

    m = run(scenario())
    nodes = {n["id"]: n for n in m["nodes"]}
    assert nodes["demo"]["kind"] == "plugin" and nodes["demo"]["jobs"] == {"active": 0, "queued": 0}
    assert nodes["w1"].get("implicit") and nodes["argus"].get("implicit")  # seen in events, not registered
    pairs = {(e["src"], e["dst"]): e["count"] for e in m["edges"]}
    assert pairs[("argus", "demo")] == 1 and pairs[("w1", "demo")] == 3  # leased, running, succeeded


# ------------------------------------------------------------------ HTTP + WebSocket


def test_status_is_public_and_event_endpoints_are_guarded(tmp_path):
    argus = make(tmp_path, token="tok").open()
    with TestClient(create_app(argus)) as c:
        s = c.get("/status").json()
        assert s["status"] == "ok" and s["token_required"] is True and s["events"]["alive"] is True
        assert s["map"]["nodes"] >= 1
        home = c.get("/lite")
        assert home.status_code == 200 and "/ws/events" in home.text
        for path in ("/events", "/map", "/registry"):
            assert c.get(path).status_code == 401
        h = {"Authorization": "Bearer tok"}
        c.post("/jobs", json={"plugin": "demo", "workflow": "echo"}, headers=h)
        body = c.get("/events?kinds=job.", headers=h).json()
        assert [e["kind"] for e in body["events"]] == ["job.queued"] and body["seq"] >= 1
        m = c.get("/map", headers=h).json()
        assert {"argus", "demo"} <= {n["id"] for n in m["nodes"]}
        assert c.get("/registry", headers=h).json()["components"]


def test_websocket_replays_then_streams_live(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        first = c.post("/jobs", json={"plugin": "demo", "workflow": "echo"}).json()["id"]
        top = argus.store.read_sync(last_seq)
        deadline = time.monotonic() + 5
        while argus.hub.cursor < top and time.monotonic() < deadline:  # let the hub pass the first job
            time.sleep(0.02)
        with c.websocket_connect("/ws/events?since=0&kinds=job.") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["seq"] >= 1
            replay = ws.receive_json()
            assert replay["replay"] is True and replay["events"][0]["job_id"] == first
            second = c.post("/jobs", json={"plugin": "demo", "workflow": "echo"}).json()["id"]
            live = ws.receive_json()
            assert live["type"] == "events" and "replay" not in live
            assert [e["job_id"] for e in live["events"]] == [second]
            assert live["events"][0]["seq"] > replay["events"][-1]["seq"]


def test_websocket_needs_token_when_set(tmp_path):
    argus = make(tmp_path, token="tok").open()
    with TestClient(create_app(argus)) as c:
        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws/events") as ws:
            ws.receive_json()
        with c.websocket_connect("/ws/events?token=tok") as ws:
            assert ws.receive_json()["type"] == "hello"
        with c.websocket_connect("/ws/events", headers={"Authorization": "Bearer tok"}) as ws:
            assert ws.receive_json()["type"] == "hello"


def test_live_event_reaches_a_real_viewer_quickly_and_tail_prints_it(tmp_path):
    from websockets.sync.client import connect

    argus = make(tmp_path, token="tok").open()
    with Server(argus) as srv:
        cl = client(srv.url, "tok")
        ws_url = srv.url.replace("http://", "ws://") + "/ws/events?kinds=job.&token=tok"
        with connect(ws_url, open_timeout=5) as ws:
            import json

            assert json.loads(ws.recv(timeout=5))["type"] == "hello"
            t0 = time.perf_counter()
            job_id = cl.post("/jobs", {"plugin": "demo", "workflow": "echo"})["id"]
            msg = json.loads(ws.recv(timeout=5))
            assert msg["events"][0]["job_id"] == job_id
            assert time.perf_counter() - t0 < 1.0  # design target: live updates under 1 s

        from argus.tail import main as tail_main

        out = io.StringIO()
        result: list[int] = []

        def run_tail():
            with redirect_stdout(out):
                result.append(tail_main(["--url", srv.url, "--token", "tok", "--since", "0", "--kinds", "job.",
                                         "--count", "1"]))

        t = threading.Thread(target=run_tail)
        t.start()
        t.join(10)
        assert result == [0] and "job.queued" in out.getvalue() and "argus -> demo" in out.getvalue()


def test_health_degraded_if_stream_stops(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        c.portal.call(argus.hub.stop)
        assert c.get("/health").status_code == 503


def test_argus_stops_quickly_with_a_viewer_still_connected(tmp_path):
    import json

    from websockets.sync.client import connect

    argus = make(tmp_path).open()
    srv = Server(argus).__enter__()
    with connect(srv.url.replace("http://", "ws://") + "/ws/events", open_timeout=5) as ws:
        assert json.loads(ws.recv(timeout=5))["type"] == "hello"
        t0 = time.perf_counter()
        srv.__exit__(None, None, None)  # the viewer is still connected
        assert not srv.thread.is_alive() and time.perf_counter() - t0 < 3.0


def test_closed_viewer_is_unsubscribed_at_once(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        with c.websocket_connect("/ws/events") as ws:
            ws.receive_json()
            assert argus.hub.stats()["subscribers"] == 1
        deadline = time.monotonic() + 2
        while argus.hub.stats()["subscribers"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert argus.hub.stats()["subscribers"] == 0


def test_component_filter_and_newest(store):
    for i in range(5):
        write(store, lambda c, i=i: insert_event(c, float(i), f"n.{i}", src="a" if i % 2 else "b", dst="c"))
    flt = EventFilter.parse("n.", None, "a")
    assert [e["kind"] for e in store.read_sync(lambda c: read_events(c, 0, flt=flt))] == ["n.1", "n.3"]
    last2 = store.read_sync(lambda c: read_events(c, 0, limit=2, flt=EventFilter.parse("n."), newest=True))
    assert [e["kind"] for e in last2] == ["n.3", "n.4"]  # the latest two, oldest first
    assert EventFilter.parse(None, None, "c").matches({"kind": "x", "job_id": None, "from": "q", "to": "c"})


def test_helios_is_served_and_home_redirects(tmp_path):
    from argus.api.app import HELIOS_DIR

    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        assert c.get("/lite").status_code == 200
        if not (HELIOS_DIR / "index.html").exists():
            pytest.skip("Helios not built into this checkout")
        r = c.get("/?token=abc", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/helios/?token=abc"
        page = c.get("/helios/")
        assert page.status_code == 200 and '<div id="root">' in page.text
        asset = page.text.split('src="')[1].split('"')[0]
        assert asset.startswith("/helios/assets/") and c.get(asset).status_code == 200
        body = c.get("/events?component=argus&newest=true&limit=5").json()
        assert body["events"] and all("argus" in (e["from"], e["to"]) for e in body["events"])
