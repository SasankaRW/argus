"""Google Calendar on the island: events and holidays normalised, merged and cached, why Google refuses in plain
words, the `calendar` plugin's sync with a fake Google (no network), and GET /island/calendar."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import urllib.parse
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from argus import gcal
from test_ask import make
from test_worker import Server, client

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t_calendar", ROOT / "plugins" / "calendar" / "plugin.py")
cal = importlib.util.module_from_spec(spec)
sys.modules["t_calendar"] = cal
spec.loader.exec_module(cal)

TODAY = date(2026, 11, 21)
HOLIDAY = {"summary": "Poya Day", "start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"}}
CHRISTMAS = {"summary": "Christmas Day", "start": {"date": "2026-12-25"}, "end": {"date": "2026-12-26"}}
DENTIST = {"summary": "Dentist", "start": {"dateTime": "2026-11-21T06:30:00+00:00"},
           "end": {"dateTime": "2026-11-21T07:15:00+00:00"}}
CAMP = {"summary": "Camp", "start": {"date": "2026-11-27"}, "end": {"date": "2026-11-30"}}  # three days


def test_normalize_gives_title_start_end_all_day_and_kind():
    ev = gcal.normalize(HOLIDAY, "holidays")
    assert ev == {"title": "Poya Day", "start": "2026-11-24", "end": "2026-11-25", "all_day": True,
                  "calendar": "holidays"}
    t = gcal.normalize(DENTIST, "events")
    assert t["all_day"] is False and t["start"].startswith("2026-11-21T06:30") and t["calendar"] == "events"
    assert gcal.normalize({"status": "cancelled", "start": {"date": "2026-11-24"}}, "events") is None
    assert gcal.normalize({"summary": "x"}, "events") is None  # no start
    assert gcal.normalize({"start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"}}, "events")["title"] \
        == "(no title)"
    assert gcal.kind_of("en.lk#holiday@group.v.calendar.google.com") == "holidays"
    assert gcal.kind_of("primary") == "events"


def test_days_an_event_touches():
    assert gcal.days_of(gcal.normalize(HOLIDAY, "holidays")) == [date(2026, 11, 24)]  # end is exclusive
    assert gcal.days_of(gcal.normalize(CAMP, "events")) == [date(2026, 11, 27), date(2026, 11, 28),
                                                            date(2026, 11, 29)]
    assert gcal.days_of({"start": "garbage", "end": "x", "all_day": False}) == []


def test_merge_sorts_dedupes_and_windows():
    a = gcal.normalize_all([DENTIST, CAMP, CHRISTMAS], "events")
    b = gcal.normalize_all([HOLIDAY, HOLIDAY, CHRISTMAS], "holidays")
    merged = gcal.merge(a, b)
    assert [e["title"] for e in merged] == ["Dentist", "Poya Day", "Camp", "Christmas Day", "Christmas Day"]
    assert [e["title"] for e in merged].count("Poya Day") == 1  # the same holiday listed twice is one
    near = gcal.window(merged, TODAY)  # today .. 30 days: Christmas (34 days) is out
    assert [e["title"] for e in near] == ["Dentist", "Poya Day", "Camp"]
    assert gcal.window(merged, date(2026, 11, 28))[0]["title"] == "Camp"  # still running that day


def test_cache_keeps_the_last_good_answer_when_a_read_fails():
    evs = gcal.normalize_all([HOLIDAY], "holidays")
    c = gcal.update_cache(None, "hol", evs, now=100.0)
    assert c["hol"] == {"events": evs, "at": 100.0, "error": None}
    c = gcal.update_cache(c, "hol", None, error="offline")
    assert c["hol"]["events"] == evs and c["hol"]["at"] == 100.0 and c["hol"]["error"] == "offline"
    c = gcal.update_cache(c, "primary", gcal.normalize_all([DENTIST], "events"), now=200.0)
    assert [e["title"] for e in gcal.cached_events(c)] == ["Dentist", "Poya Day"]  # both calendars together
    c = gcal.update_cache(c, "hol", [], now=300.0)  # a good empty answer does replace it
    assert [e["title"] for e in gcal.cached_events(c)] == ["Dentist"]


def test_google_refusals_in_plain_words():
    off = {"error": {"code": 403, "message": "Google Calendar API has not been used in project 1 before or it is "
                                             "disabled.", "errors": [{"reason": "accessNotConfigured"}]}}
    assert "isn't switched on" in gcal.explain(403, json.dumps(off).encode())
    assert "took the OK back" in gcal.explain(400, b'{"error": "invalid_grant", "error_description": "Token has been '
                                                   b'expired or revoked."}')
    assert "took the OK back" in gcal.explain(401, b"{}")
    scope = {"error": {"message": "Request had insufficient authentication scopes.",
                       "errors": [{"reason": "insufficientPermissions"}]}}
    assert "calendar.readonly" in gcal.explain(403, scope)
    assert "no calendar called nope" in gcal.explain(404, b"{}", "nope")
    assert gcal.explain(500, b"not json") == "Google answered 500."


def test_the_islands_answer_and_month_marks():
    state = {"status": {"state": "ok"},
             "cache": {"primary": {"events": gcal.normalize_all([DENTIST, CAMP], "events"), "at": 5.0, "error": None},
                       "hol": {"events": gcal.normalize_all([HOLIDAY, CHRISTMAS], "holidays"), "at": 7.0,
                               "error": None}}}
    got = gcal.for_island(state, TODAY)
    assert set(got) == {"connected", "updated", "events"} and got["connected"] and got["updated"] == 7.0
    assert [e["title"] for e in got["events"]] == ["Dentist", "Poya Day", "Camp"]
    assert set(got["events"][0]) == {"title", "start", "end", "all_day", "calendar"}
    m = gcal.marks(got["events"], 2026, 11)
    assert m[24] == {"holidays"} and m[27] == m[28] == m[29] == {"events"} and 25 not in m
    assert [e["title"] for e in gcal.on_day(got["events"], date(2026, 11, 28))] == ["Camp"]
    assert gcal.clock(got["events"][1]) == "" and gcal.clock(got["events"][0]) != ""
    # not connected, or never read: nothing, and no error
    nothing = {"connected": False, "updated": None, "events": []}
    assert gcal.for_island(None, TODAY) == nothing
    assert gcal.for_island({"status": {"state": "needs_connect"}, "cache": state["cache"]}, TODAY) == nothing
    assert gcal.for_island({"status": {"state": "error", "message": "x"}, "cache": {}}, TODAY) == nothing
    # a read that failed this time still shows the last good events
    stale = {"status": {"state": "error"}, "cache": {"primary": {**state["cache"]["primary"], "error": "offline"}}}
    assert gcal.for_island(stale, TODAY)["connected"]


# ---------------------------------------------------------------- the plugin, with a fake Google

class FakeGoogle:
    def __init__(self, replies):
        self.replies = replies  # calendar id -> (status, body) or an exception
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, *, data=None, headers=None, content_type=None, **kw):
        form = dict(urllib.parse.parse_qsl(data.decode())) if data else {}
        self.calls.append((method, url, form))
        if url == cal.TOKEN:
            return 200, json.dumps({"access_token": "A2", "expires_in": 3600}).encode()
        assert headers["Authorization"] == "Bearer A1" or headers["Authorization"] == "Bearer A2"
        if url.startswith(cal.LIST):
            return self.replies["__list__"]
        cid = urllib.parse.unquote(url[len(cal.API) + 1:].split("/events")[0])
        r = self.replies[cid]
        if isinstance(r, Exception):
            raise r
        return r


def ctx(tmp_path, google, *, token=True, calendars=None):
    st: dict = {}
    if token:
        (tmp_path / "token.json").write_text(json.dumps({"access_token": "A1", "refresh_token": "R1",
                                                         "expires_at": time.time() + 3000}))
    env = {"GMAIL_CLIENT_ID": "cid.apps.googleusercontent.com", "GMAIL_CLIENT_SECRET": "sec"}
    return SimpleNamespace(
        input={}, dry_run=False, config={"chrome_profile": "Sasanka", "days": 30,
                                         "calendars": calendars or ["primary", HOL_ID]}, data_dir=tmp_path,
        secrets=SimpleNamespace(get=lambda k, d=None: env.get(k, d)), http=google,
        store=SimpleNamespace(get=lambda k, d=None: st.get(k, d), set=lambda k, v: st.__setitem__(k, v), data=st),
        step=lambda name, fn, *a, **k: fn(*a, **k))


HOL_ID = "en.lk#holiday@group.v.calendar.google.com"


def ok(*items):
    return 200, json.dumps({"items": list(items)}).encode()


def test_sync_reads_both_calendars_with_the_right_scope_window_and_ids(tmp_path):
    g = FakeGoogle({"primary": ok(DENTIST, {"status": "cancelled", "start": {"date": "2026-11-22"}}),
                    HOL_ID: ok(HOLIDAY)})
    c = ctx(tmp_path, g)
    out = cal.sync(c)
    assert out == {"done": True, "events": 2}
    assert cal.SCOPE == "https://www.googleapis.com/auth/calendar.readonly"
    urls = [u for _, u, _ in g.calls]
    assert urls[0].startswith(cal.API + "/primary/events?") and "singleEvents=true" in urls[0]
    assert urllib.parse.quote(HOL_ID, safe="") in urls[1]  # the id is escaped in the address
    assert c.store.data["status"]["state"] == "ok"
    got = gcal.for_island({"status": c.store.data["status"], "cache": c.store.data["cache"]}, TODAY)
    assert got["connected"] and {e["calendar"] for e in got["events"]} == {"events", "holidays"}
    assert cal.sync(SimpleNamespace(**{**c.__dict__, "dry_run": True}))["dry_run"]


def test_a_failed_read_keeps_the_last_events_and_says_why(tmp_path):
    g = FakeGoogle({"primary": ok(DENTIST), HOL_ID: ok(HOLIDAY)})
    c = ctx(tmp_path, g)
    cal.sync(c)
    off = {"error": {"message": "Google Calendar API has not been used", "errors": [{"reason": "accessNotConfigured"}]}}
    g.replies = {"primary": (403, json.dumps(off).encode()), HOL_ID: OSError("offline")}
    out = cal.sync(c)
    assert not out["done"] and "isn't switched on" in out["problem"]
    assert c.store.data["status"]["state"] == "error"
    assert "isn't switched on" in c.store.data["status"]["message"]
    assert len(gcal.cached_events(c.store.data["cache"])) == 2  # still there, offline or refused
    assert c.store.data["cache"][HOL_ID]["error"] == cal.OFFLINE


def test_no_key_means_needs_connect_and_the_schedule_never_opens_chrome(tmp_path):
    g = FakeGoogle({})
    c = ctx(tmp_path, g, token=False)
    opened = []
    c.open_in_chrome = lambda url, profile: opened.append(url)
    out = cal.sync(c)  # the 15-minute job
    assert not out["done"] and not opened and "Connect Google Calendar" in out["problem"]
    assert c.store.data["status"]["state"] == "needs_connect"
    assert gcal.for_island({"status": c.store.data["status"], "cache": {}}, TODAY)["connected"] is False
    c.after_job_http = g
    cal._PENDING.clear()
    out = cal.connect(c)  # the button: Google's page opens, with its own scope
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))
    assert out["allow"] and q["scope"] == cal.SCOPE and q["access_type"] == "offline"
    assert cal.token_path(c).name == "token.json" and "gmail" not in str(cal.token_path(c))
    cal._PENDING["flow"]["done"].set()


def test_a_revoked_key_or_missing_scope_opens_google_again_from_the_button(tmp_path):
    g = FakeGoogle({"primary": (401, b"{}"), HOL_ID: ok()})
    c = ctx(tmp_path, g)
    opened = []
    c.open_in_chrome = lambda url, profile: opened.append(url)
    c.after_job_http = g
    cal._PENDING.clear()
    out = cal.sync(c)  # the schedule: says so, never opens Chrome
    assert not out["done"] and "took the OK back" in out["problem"] and not opened
    assert c.store.data["status"]["state"] == "needs_connect"
    out = cal.connect(c)  # the button: a key is saved but refused, so Google's page opens again
    assert out["allow"] and len(opened) == 1 and "click Allow" in out["problem"]
    cal._PENDING["flow"]["done"].set()
    scope = {"error": {"message": "Request had insufficient authentication scopes.",
                       "errors": [{"reason": "insufficientPermissions"}]}}
    assert gcal.needs_new_key(403, json.dumps(scope).encode()) and gcal.needs_new_key(401, b"{}")
    assert not gcal.needs_new_key(403, b'{"error": {"errors": [{"reason": "accessNotConfigured"}]}}')
    assert not gcal.needs_new_key(404, b"{}")
    g.replies = {"primary": (403, json.dumps(scope).encode()), HOL_ID: ok()}
    cal._PENDING.clear()
    assert cal.connect(c)["allow"] and len(opened) == 2
    cal._PENDING["flow"]["done"].set()


def test_the_plugin_loads_and_its_default_calendars_are_a_setting(tmp_path, monkeypatch):
    with Server(make(tmp_path, monkeypatch).open()) as srv:
        d = client(srv.url).get("/plugins/calendar")
    cfg = {s["name"]: s for s in d["settings"]}
    assert cfg["calendars"]["default"] == ["all", HOL_ID]
    assert [b["label"] for b in d["buttons"]] == ["Connect Google Calendar", "Refresh now"]
    assert any(s["workflow"] == "sync" and s["cron"] == "*/15 * * * *" for s in d["schedules"])


def test_island_calendar_endpoint_shape(tmp_path, monkeypatch):
    with Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        assert cl.get("/island/calendar") == {"connected": False, "updated": None, "events": []}
        today = date.today()
        mine = {"summary": "Dentist", "start": {"date": today.isoformat()},
                "end": {"date": date.fromordinal(today.toordinal() + 1).isoformat()}}
        cache = gcal.update_cache(None, "primary", gcal.normalize_all([mine], "events"), now=time.time())
        cl.call("PUT", "/plugins/calendar/state/cache", {"value": cache})
        cl.call("PUT", "/plugins/calendar/state/status", {"value": {"state": "ok", "message": "Connected."}})
        got = cl.get("/island/calendar")
        assert got["connected"] is True and isinstance(got["updated"], float)
        assert got["events"] == [{"title": "Dentist", "start": today.isoformat(),
                                  "end": date.fromordinal(today.toordinal() + 1).isoformat(), "all_day": True,
                                  "calendar": "events"}]
        cl.call("PUT", "/plugins/calendar/state/status", {"value": {"state": "needs_connect", "message": "x"}})
        assert cl.get("/island/calendar")["events"] == []  # not connected: nothing, no error


def test_the_island_draws_the_calendar_with_and_without_events(tmp_path):
    import subprocess

    base = [sys.executable, "-m", "argus.ari.ari_popup", "--details", "--page", "calendar", "--url",
            "http://127.0.0.1:9"]
    from argus.ari.ari_popup import sample_calendar

    s = sample_calendar(datetime(2026, 11, 21, 9, 0))
    assert s["connected"] and {e["calendar"] for e in s["events"]} == {"events", "holidays"}
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return
    for extra in ([], ["--text", "nocal"]):
        png = tmp_path / f"cal{len(extra)}.png"
        r = subprocess.run([*base, "--preview", str(png), *extra], capture_output=True, text=True, timeout=120,
                           cwd=ROOT / "core", env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"})
        assert r.returncode == 0 and png.stat().st_size > 1000, r.stderr[-400:]


def test_all_means_every_calendar_ticked_in_google_calendar():
    listed = [{"id": "me@example.com", "selected": True, "primary": True}, {"id": "f1", "selected": True},
              {"id": "hidden", "selected": True, "hidden": True}, {"id": "unticked"}, {"id": HOL_ID, "selected": True}]
    assert gcal.expand_calendars(["all", HOL_ID], listed) == ["me@example.com", "f1", HOL_ID]
    assert gcal.expand_calendars(["primary", "f1"], listed) == ["primary", "f1"]  # no "all": as written
    assert gcal.expand_calendars(["all", HOL_ID], None) == ["primary", HOL_ID]  # the list failed: yours at least


def test_sync_reads_the_ticked_calendars_and_never_keeps_the_key_in_a_step(tmp_path):
    timetable = {"summary": "Lecture", "start": {"dateTime": "2026-11-23T08:00:00+05:30"},
                 "end": {"dateTime": "2026-11-23T10:00:00+05:30"}}
    g = FakeGoogle({"__list__": ok({"id": "tt", "selected": True}, {"id": HOL_ID, "selected": True}),
                    "tt": ok(timetable), HOL_ID: ok(HOLIDAY)})
    c = ctx(tmp_path, g, calendars=["all", HOL_ID])
    steps = []
    c.step = lambda name, fn, *a, **k: (steps.append((name, fn(*a, **k))), steps[-1][1])[1]
    assert cal.sync(c) == {"done": True, "events": 2}
    assert "A1" not in json.dumps(steps, default=str)  # a job's steps are shown in Helios: no access key there
    assert gcal.DEFAULT_CALENDARS[0] == "all"
