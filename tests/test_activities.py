"""Live activities: ranking, expiry, coalesced events, long-poll wake, job promotion, timers, custom auth and
transfers derived from Send."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from argus import activities as activities_mod
from argus.activities import MIN_EMIT, Activities, rank
from argus.api import create_app
from argus.send_live import SendLive
from conftest import run
from test_send_api import make

W = {"Authorization": "Bearer t0ken"}


def snap(**over):
    return {"offer_id": "o" * 8, "leg": "send", "from": "phone", "to": "pc", "label": "a.pdf", "files": 1,
            "total": 100, "done": 40, "state": "moving", "speed": 10, "eta": 6, **over}


def item(kind, started, **over):
    return {"id": f"{kind}:{started}", "kind": kind, "title": kind, "detail": "", "icon": "x", "progress": None,
            "ends_at": None, "started_at": started, **over}


def test_ranking_order_and_ties():
    now = 1000.0
    items = [item("custom", 1), item("job", 2), item("timer", 3, ends_at=now + 600), item("transfer", 4),
             item("timer", 5, ends_at=now + 30), item("job", 9)]
    got = [(a["kind"], a["started_at"]) for a in rank(items, now)]
    assert got == [("timer", 5), ("transfer", 4), ("timer", 3), ("job", 9), ("job", 2), ("custom", 1)]
    assert [a["priority"] for a in rank(items, now)] == [5, 4, 3, 2, 2, 1]


def test_custom_expires_and_survives_updates(clock):
    a = Activities(clock=clock)
    a.put_custom("rest", {"title": "Rest", "ends_at": clock.now + 90, "progress": None})
    assert [i["id"] for i in a.snapshot()["items"]] == ["custom:rest"]
    clock.advance(140)  # ends_at + 60 s not yet reached
    assert a.snapshot()["items"]
    clock.advance(20)
    assert a.snapshot()["items"] == []
    a.put_custom("note", {"title": "Idle one"})  # no end time: goes after 10 min without an update
    clock.advance(500)
    a.put_custom("note", {"title": "Idle one"})
    clock.advance(500)
    assert len(a.snapshot()["items"]) == 1
    clock.advance(200)
    assert a.snapshot()["items"] == []
    assert not a.delete_custom("note")


def test_version_moves_only_on_real_changes_and_events_coalesce(clock):
    a = Activities(clock=clock)
    v0 = a.snapshot()["version"]
    assert a.snapshot()["version"] == v0 and a.take_events() == []
    for n in range(5):  # a burst of changes inside one window: one event
        a.put_custom("c", {"title": f"t{n}"})
    assert a.version == v0 + 5
    assert a.take_events() == [("activity.changed", {"version": v0 + 5})]
    a.put_custom("c", {"title": "again"})
    assert a.take_events() == []  # too soon after the last one
    clock.advance(MIN_EMIT + 0.01)
    assert a.take_events() == [("activity.changed", {"version": v0 + 6})]
    assert a.take_events() == []


def test_long_poll_wakes_on_change_and_times_out(clock):
    a = Activities(clock=clock)

    async def go():
        v = a.snapshot()["version"]
        task = asyncio.create_task(a.changed(v, 5))
        await asyncio.sleep(0.05)
        assert not task.done()
        a.put_custom("x", {"title": "X"})
        got = await asyncio.wait_for(task, 2)
        assert got["version"] == v + 1 and got["items"][0]["id"] == "custom:x"
        assert (await a.changed(v, 5))["version"] == v + 1  # already behind: answers at once
        quiet = await a.changed(v + 1, 0.05)
        assert quiet["version"] == v + 1

    run(go())


def test_transfers_are_derived_from_send_live(clock):
    live = SendLive()
    a = Activities(live, clock)
    assert a.snapshot()["items"] == []
    live.report("phone", [snap()], clock.now)
    a.poke()
    [t] = a.snapshot()["items"]
    assert t["kind"] == "transfer" and t["id"] == f"transfer:{'o' * 8}:send" and t["progress"] == 0.4
    assert t["title"] == "a.pdf to Pc" and t["detail"] == "40% · 6 s left" and t["device"] == "pc"
    a.set_names({"pc": "PC"})
    assert a.snapshot()["items"][0]["title"] == "a.pdf to PC"
    v = a.version
    live.report("phone", [snap(eta=5)], clock.now)
    a.poke()
    assert a.version == v  # only the eta text moved
    live.report("phone", [snap(done=60, eta=4)], clock.now)
    a.poke()
    assert a.version == v + 1
    live.report("pc", [snap(done=100, state="done")], clock.now)
    a.poke()
    assert a.snapshot()["items"] == []  # finished: gone from the live list


def test_job_is_promoted_after_ten_seconds(jobs, clock, monkeypatch):
    monkeypatch.setattr(activities_mod, "SHOW_JOBS", True)  # off by default (Sas): the mechanism still works
    a = Activities(clock=clock)
    jobs.on_event = a.job_event

    async def go():
        jid, _ = await jobs.enqueue("organizer", "sort_downloads")
        await jobs.claim("w", {"cpu"})
        await jobs.start(jid, "w")
        await jobs.record_step(jid, "w", 1, "scan", state="running")
        clock.advance(9)
        assert a.snapshot()["items"] == []
        clock.advance(1.5)
        [j] = a.snapshot()["items"]
        assert j["kind"] == "job" and j["id"] == f"job:{jid}" and j["progress"] is None
        assert j["title"] == "Organizer: sort downloads" and j["detail"] == "scan · step 2"
        await jobs.record_step(jid, "w", 2, "move", state="running")
        assert a.snapshot()["items"][0]["detail"] == "move · step 3"
        await jobs.succeed(jid, "w")
        assert a.snapshot()["items"] == []
        # a quick job never shows
        j2, _ = await jobs.enqueue("organizer", "quick")
        await jobs.claim("w", {"cpu"})
        await jobs.start(j2, "w")
        clock.advance(3)
        await jobs.succeed(j2, "w")
        clock.advance(20)
        assert a.snapshot()["items"] == []

    run(go())


def test_a_succeeded_phone_timer_becomes_a_timer_that_ends_itself(jobs, clock):
    a = Activities(clock=clock)
    jobs.on_event = a.job_event

    async def go():
        jid, _ = await jobs.enqueue("phone", "timer", {"minutes": 2, "label": "Tea"})
        await jobs.claim("w", {"cpu"})
        await jobs.start(jid, "w")
        await jobs.succeed(jid, "w", {"minutes": 2})

    run(go())
    [t] = a.snapshot()["items"]
    assert (t["kind"], t["title"], t["device"], t["icon"]) == ("timer", "Tea", "phone", "timer")
    assert t["ends_at"] == clock.now + 120 and t["started_at"] == clock.now
    clock.advance(121)
    assert "activity.done" in dict(a.take_events())
    assert a.snapshot()["items"]  # lingers a few seconds past its end
    clock.advance(6)
    assert a.snapshot()["items"] == []


def test_timer_done_event_fires_once(clock):
    a = Activities(clock=clock)
    t = a.start_timer(1, "Pasta")
    assert t["kind"] == "timer" and t["device"] == "pc" and t["ends_at"] == clock.now + 60
    a.take_events()
    clock.advance(61)
    kinds = [k for k, _ in a.take_events()]
    assert kinds.count("activity.done") == 1
    clock.advance(1)
    assert "activity.done" not in [k for k, _ in a.take_events()]


def test_the_soonest_timer_wins_and_the_order_follows_time(clock):
    a = Activities(SendLive(), clock)
    a.start_timer(10, "Long")
    a.put_custom("c", {"title": "C"})
    assert [i["kind"] for i in a.snapshot()["items"]] == ["timer", "custom"]
    a.put_custom("c2", {"title": "C2"})
    v = a.version
    clock.advance(10 * 60 - 30)  # the timer is now under a minute away: still first, and the order is re-ranked
    assert a.snapshot()["items"][0]["priority"] == 5 and a.version > v


def test_the_routes_and_their_auth(tmp_path):
    argus = make(tmp_path)
    with TestClient(create_app(argus)) as c:
        named = c.post("/tokens", json={"name": "workout"}, headers=W).json()["token"]
        N = {"Authorization": f"Bearer {named}"}
        body = {"title": "Rest", "detail": "Bench press", "icon": "timer", "ends_at": 3.9e9}
        assert c.put("/activities/custom/workout-rest", json=body).status_code == 401
        assert c.delete("/activities/custom/workout-rest").status_code == 401
        assert c.get("/activities").status_code == 401
        assert c.post("/activities/timer", json={"minutes": 1}).status_code == 401
        assert c.put("/activities/custom/workout-rest", json=body, headers=W).status_code == 200
        r = c.put("/activities/custom/other", json={"title": "Two"}, headers=N)  # a named token works too
        assert r.status_code == 200 and r.json()["id"] == "custom:other"
        got = c.get("/activities", headers=N).json()
        assert {i["id"] for i in got["items"]} == {"custom:workout-rest", "custom:other"} and got["version"] >= 2
        assert c.get(f"/activities?after={got['version']}&wait=0.2", headers=W).json()["version"] == got["version"]
        assert c.delete("/activities/custom/other", headers=N).json() == {"ok": True}
        assert c.delete("/activities/custom/other", headers=N).status_code == 404
        assert c.put("/activities/custom/bad id", json=body, headers=W).status_code == 422  # not a valid id
        assert c.put("/activities/custom/x", json={"title": "", "progress": 3}, headers=W).status_code == 422
        assert c.put("/activities/custom/x", json={"title": "x", "ends_at": 1.7e12}, headers=W).status_code == 422  # ms
        t = c.post("/activities/timer", json={"minutes": 5, "label": "Tea"}, headers=W).json()
        assert t["kind"] == "timer" and t["title"] == "Tea"
        assert c.post("/activities/timer", json={"minutes": 0}, headers=W).status_code == 422
        assert c.delete("/activities/custom/workout-rest", headers=W).status_code == 200
        assert c.delete("/tokens/workout", headers=W).status_code == 200
        assert c.put("/activities/custom/x", json=body, headers=N).status_code == 401  # revoked


def test_changes_reach_the_hub_as_events(tmp_path):
    import time

    with TestClient(create_app(make(tmp_path))) as c:
        c.put("/activities/custom/e", json={"title": "E"}, headers=W)
        for _ in range(60):
            evs = c.get("/events?kinds=activity.&limit=10", headers=W).json()["events"]
            if evs:
                break
            time.sleep(0.1)
        assert evs and evs[0]["kind"] == "activity.changed" and evs[0]["data"]["version"] >= 1


def test_a_malformed_send_report_is_survived(tmp_path):
    import time

    with TestClient(create_app(make(tmp_path))) as c:
        r = c.post("/send/live", json={"device": "phone", "items": [{"offer_id": "x"}]}, headers=W)
        assert r.status_code == 200
        bad = {"offer_id": "y" * 8, "from": "phone", "to": "pc", "state": "moving", "total": "lots", "done": None,
               "eta": "soon"}
        assert c.post("/send/live", json={"device": "phone", "items": [bad]}, headers=W).status_code == 200
        assert c.post("/send/live", json={"device": "phone", "items": [bad]}, headers=W).status_code == 200
        assert c.get("/activities", headers=W).status_code == 200
        ok = {"offer_id": "z" * 8, "from": "phone", "to": "pc", "state": "moving", "total": 100, "done": 50}
        assert c.post("/send/live", json={"device": "phone", "items": [ok]}, headers=W).status_code == 200
        ids = [i["id"] for i in c.get("/activities", headers=W).json()["items"]]
        assert f"transfer:{'z' * 8}:send" in ids
        n = len(c.get("/events?kinds=activity.&limit=50", headers=W).json()["events"])
        c.put("/activities/custom/after", json={"title": "After"}, headers=W)  # the pump still emits
        for _ in range(60):
            if len(c.get("/events?kinds=activity.&limit=50", headers=W).json()["events"]) > n:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("pump stopped emitting")


def test_transfer_activity_coerces_and_a_bad_one_is_skipped(clock):
    from argus.activities import transfer_activity

    t = transfer_activity({"offer_id": "x"})
    assert t["progress"] == 0.0 and t["detail"] == "0%"
    t = transfer_activity({"offer_id": "x", "total": "9", "done": "3", "eta": "nan"})
    assert abs(t["progress"] - 1 / 3) < 1e-9 and t["detail"] == "33%"
    live = SendLive()
    a = Activities(live, clock)
    live._items[("q", "send")] = {"offer_id": "q", "state": "moving", "to": _Boom(), "updated": clock.now,
                                  "started": clock.now}
    live.report("phone", [snap()], clock.now)
    assert [i["kind"] for i in a.snapshot()["items"]] == ["transfer"]


def test_a_failed_write_is_retried(clock):
    a = Activities(clock=clock)
    a.put_custom("c", {"title": "x"})
    evs = a.take_events()
    assert [k for k, _ in evs] == ["activity.changed"]
    a.requeue(evs)  # the write failed
    assert [k for k, _ in a.take_events()] == ["activity.changed"]  # same window, retried at once
    a.start_timer(1)
    clock.advance(61)
    evs = a.take_events()
    assert "activity.done" in [k for k, _ in evs]
    a.requeue(evs)
    assert [k for k, _ in a.take_events() if k == "activity.done"] == ["activity.done"]


def test_pump_survives_failing_writes_and_retries(clock):
    a = Activities(clock=clock)
    got: list = []
    calls = {"n": 0}

    async def write(evs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("hub down")
        got.extend(evs)

    async def go():
        t = asyncio.create_task(a.pump(write))
        await asyncio.sleep(0)
        a.put_custom("c", {"title": "x"})
        for _ in range(40):
            if got:
                break
            clock.advance(MIN_EMIT + 0.01)
            await asyncio.sleep(0.05)
        t.cancel()

    run(go())
    assert calls["n"] >= 2 and got and got[0][0] == "activity.changed"


def test_small_progress_changes_do_not_bump_the_version(clock):
    a = Activities(clock=clock)
    a.put_custom("c", {"title": "t", "progress": 0.50})
    v = a.version
    a.put_custom("c", {"title": "t", "progress": 0.52})
    assert a.version == v
    a.put_custom("c", {"title": "t", "progress": 0.60})
    assert a.version == v + 1


def test_the_pump_sleeps_long_when_nothing_needs_it(clock):
    a = Activities(clock=clock)
    a.take_events()
    assert a.wait_time() == 5.0
    a.start_timer(10)
    a.take_events()
    assert 4.9 < a.wait_time() <= 5.0  # a far-off timer: no 4 Hz ticking
    clock.advance(10 * 60 - 1)
    assert a.wait_time() == MIN_EMIT  # within 2 s of its end


class _Boom:
    def __str__(self):
        raise ValueError("unprintable")


def test_jobs_are_not_shown_by_default():
    """Sas: every plugin job on the island was noise; only transfers and timers are live activities."""
    from types import SimpleNamespace

    assert activities_mod.SHOW_JOBS is False
    t = [1000.0]
    a = Activities(clock=lambda: t[0])
    a.job_event("job.running", SimpleNamespace(id="j1", plugin="vault", workflow="tidy", input={}), t[0])
    t[0] += 60
    assert [x for x in a.snapshot()["items"] if x["kind"] == "job"] == []
