"""Life Hub and Tracker on the live map: down only after 2 failures in a row, since the first one, one short line."""

from __future__ import annotations

import asyncio

from argus.apphealth import (
    AppHealth,
    State,
    check_one,
    is_down,
    life_hub_line,
    node_fields,
    step,
    tracker_line,
)
from test_approvals import make_argus


def test_one_failure_is_not_down_two_are_and_since_is_the_first():
    s = step(State(), True, 100.0, line="All synced")
    assert not is_down(s) and s.up is True
    s = step(s, False, 220.0, "refused")
    assert not is_down(s) and s.fails == 1 and s.since == 220.0
    assert node_fields(s)["status"] == "up"
    s = step(s, False, 340.0, "timed out")
    assert is_down(s) and s.since == 220.0 and s.error == "timed out"
    f = node_fields(s)
    assert f["status"] == "down" and f["since"] == 220.0 and f["line"] == "Not answering"
    s = step(s, False, 460.0, "timed out")
    assert s.since == 220.0 and s.fails == 3


def test_a_good_answer_clears_it_and_a_blip_keeps_the_old_since():
    s = step(State(), True, 100.0)
    s = step(s, False, 220.0, "x")
    s = step(s, True, 340.0)  # recovered after one miss
    assert s.fails == 0 and s.up is True and s.since == 340.0
    s = step(step(s, False, 460.0, "x"), False, 580.0, "x")
    s = step(s, True, 700.0)
    assert not is_down(s) and node_fields(s)["since"] is None and node_fields(s)["error"] == ""


def test_first_failures_before_any_answer_are_checking_then_down():
    s = step(State(), False, 10.0, "refused")
    assert node_fields(s)["status"] == "checking"
    s = step(s, False, 130.0, "refused")
    assert node_fields(s)["status"] == "down" and node_fields(s)["since"] == 10.0
    assert node_fields(State())["status"] == "checking"


def test_lines():
    news = [{"kind": "back_in_stock"}, {"kind": "price_drop"}, {"kind": "back_in_stock"}]
    assert life_hub_line(news) == "2 back in stock"
    assert life_hub_line([{"kind": "price_drop"}]) == "1 wishlist update"
    assert life_hub_line([]) == "All synced"
    assert life_hub_line(None) == "Up"
    assert tracker_line({"open": 7}) == "7 open issues"
    assert tracker_line({"open": 1}) == "1 open issue"
    assert tracker_line({"open": 0}) == "No open issues"
    assert tracker_line({}) == "Up"


def fake(answers):
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        a = answers[url.split(":", 2)[2].split("/", 1)[1]]
        if isinstance(a, Exception):
            raise a
        return a

    return fetch, calls


def test_check_one_uses_health_for_up_and_the_key_for_the_line():
    fetch, calls = fake({"api/health": {"ok": True}, "api/advisor/news": [{"kind": "back_in_stock"}]})
    assert check_one("lifehub", "http://x:8081/", "k", fetch) == (True, "", "1 back in stock")
    assert calls[0] == ("http://x:8081/api/health", {}) and calls[1][1] == {"X-API-Key": "k"}
    fetch, _ = fake({"api/health": {"ok": True}, "api/summary": {"open": 3}})
    assert check_one("tracker", "http://x:8080", "k", fetch) == (True, "", "3 open issues")


def test_check_one_still_up_when_only_the_line_fails_or_no_key():
    fetch, calls = fake({"api/health": {"ok": True}, "api/summary": OSError("boom")})
    assert check_one("tracker", "http://x:8080", "k", fetch) == (True, "", "Up")
    assert check_one("tracker", "http://x:8080", "", fetch) == (True, "", "Up")
    assert len(calls) == 3  # the keyless one only asked /api/health


def test_check_one_down_when_health_fails():
    fetch, _ = fake({"api/health": ConnectionRefusedError("refused")})
    ok, err, line = check_one("lifehub", "http://x:8081", "k", fetch)
    assert (ok, err, line) == (False, "refused", None)


def test_appHealth_tracks_a_target_over_time_and_drops_it_when_off():
    t = [1000.0]
    on = {"lifehub": ("http://x:8081", "k")}
    fetch, _ = fake({"api/health": OSError("refused")})
    h = AppHealth(lambda: on, clock=lambda: t[0], fetch=fetch)
    assert h.nodes()[0]["status"] == "checking"

    async def go():
        await h.tick()  # starts a check and returns at once
        await h._task
        await h.tick()  # too soon: not asked again
        assert h._task.done() and h.states["lifehub"].fails == 1
        t[0] += 121
        await h.tick()
        await h._task

    asyncio.run(go())
    n = h.nodes()[0]
    assert (n["id"], n["kind"], n["status"], n["since"], n["every"]) == ("app-lifehub", "app", "down", 1000.0, 120)
    on.clear()
    assert h.nodes() == []  # plugin off or no address: no box at all
    asyncio.run(h.check())
    assert h.states == {}


def test_the_map_carries_the_app_nodes(tmp_path):
    argus = make_argus(tmp_path).open()
    argus.registry.app_nodes = lambda: [{"id": "app-tracker", "kind": "app", "label": "Tracker", "group": None,
                                         "meta": {}, "first_seen": 1.0, "status": "up", "line": "2 open issues"}]
    nodes = asyncio.run(argus.registry.map())["nodes"]
    assert [n for n in nodes if n["id"] == "app-tracker"][0]["line"] == "2 open issues"


def test_a_plugin_stopped_in_helios_has_no_box_and_the_dry_run_switch_does_not_matter(tmp_path):
    argus = make_argus(tmp_path).open()
    argus.registry.app_nodes = lambda: [{"id": "app-tracker", "kind": "app", "label": "Tracker", "plugin": "tracker",
                                         "group": None, "meta": {}, "first_seen": 1.0, "status": "up"}]

    def ids():
        return [n["id"] for n in asyncio.run(argus.registry.map())["nodes"]]

    assert "app-tracker" in ids()
    asyncio.run(argus.store.write(lambda conn: conn.execute(
        "INSERT INTO plugin_control (plugin, paused, since, updated_at) VALUES ('tracker', 1, 5, 5)")))
    assert "app-tracker" not in ids()


def test_targets_need_the_plugin_loaded_and_an_address(tmp_path):
    argus = make_argus(tmp_path).open()
    argus.cfg.secrets.env["TRACKER_API_KEY"] = "k"
    plugins = argus.plugin_host.plugins
    assert argus._app_targets() == {}  # nothing loaded in the test config
    from types import SimpleNamespace as P
    plugins["tracker"] = P(live=False, config={"url": " http://127.0.0.1:8080 "})  # dry-run still counts
    plugins["lifehub"] = P(live=True, config={"url": ""})
    assert argus._app_targets() == {"tracker": ("http://127.0.0.1:8080", "k")}
