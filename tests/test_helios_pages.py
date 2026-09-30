"""Helios's Settings, Inbox and Models pages: their API."""

from __future__ import annotations

import pytest

from argus.worker import Worker
from argus.worker.client import ApiError
from test_ask import make
from test_worker import Server, client, wait_for


def test_settings_change_now_survive_a_restart_and_go_back(tmp_path, monkeypatch):
    a = make(tmp_path, monkeypatch)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        rows = {s["key"]: s for s in cl.get("/argus-settings")["settings"]}
        assert rows["summary.at"]["type"] == "time" and rows["power.mode"]["options"] == ["simulated", "real"]
        assert rows["backup.keep"]["type"] == "int" and rows["backup.keep"]["min"] == 1
        assert rows["brief.enabled"]["type"] == "bool" and not rows["brief.enabled"]["changed"]
        # a bad value: refused with the reason, nothing changed (not even the good one sent with it)
        with pytest.raises(ApiError) as e:
            cl.call("PUT", "/argus-settings", {"summary.at": "21:15", "backup.keep": 0})
        assert e.value.status == 422 and "backup.keep" in str(e.value)
        assert a.cfg.summary.at == "20:00"
        with pytest.raises(ApiError):
            cl.call("PUT", "/argus-settings", {"instance.name": "x"})  # not offered here
        rows = {s["key"]: s for s in cl.call("PUT", "/argus-settings",
                                             {"summary.at": "21:15", "backup.keep": 9, "claude.calls_per_day": 5})[1]
                ["settings"]}
        assert rows["summary.at"]["value"] == "21:15" and rows["summary.at"]["changed"]
        assert rows["summary.at"]["default"] == "20:00"
        assert a.cfg.summary.at == "21:15" and a.backups.keep == 9  # in effect at once
        assert cl.get("/models")["claude"]["calls_per_day"] == 5
    # a restart keeps them (argus.yaml unchanged)
    from argus.config import load_config
    from argus.context import Argus
    b = Argus(load_config(tmp_path / "argus.yaml"))
    with Server(b.open()) as srv:
        cl = client(srv.url)
        assert b.cfg.summary.at == "21:15" and b.cfg.claude.calls_per_day == 5 and b.backups.keep == 9
        cl.call("PUT", "/argus-settings", {"summary.at": None, "backup.keep": 5})  # back; same as argus.yaml
        rows = {s["key"]: s for s in cl.get("/argus-settings")["settings"]}
        assert b.cfg.summary.at == "20:00" and not rows["summary.at"]["changed"]
        assert not rows["backup.keep"]["changed"] and rows["claude.calls_per_day"]["changed"]


def test_inbox_has_approvals_and_aris_questions(tmp_path, monkeypatch):
    a = make(tmp_path, monkeypatch)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        assert cl.get("/inbox") == {"items": [], "counts": {"approval": 0, "lesson": 0, "question": 0}}
        conv = cl.post("/ari", {"text": "sort my downloads every morning at 7"})["conv"]
        w = Worker(cl, "w", watch_folders=False)
        w.register()
        jid = cl.post("/jobs", {"plugin": "demo", "workflow": "approval", "input": {"vendor": "CEB", "amount": "10"}})
        assert w.run_once(wait=2)
        wait_for(lambda: cl.get(f"/jobs/{jid['id']}")["state"] == "waiting")
        box = cl.get("/inbox")
        assert [i["kind"] for i in box["items"]] == ["approval", "question"]
        assert box["items"][0]["title"] == "CEB bill" and box["items"][0]["plugin"] == "demo"
        q = box["items"][1]
        assert q["id"] == conv and q["chat"] == "sort my downloads every morning at 7" and "7 am" in q["title"]
        cl.post(f"/ari/{conv}/answer", {"yes": False})
        assert [i["kind"] for i in cl.get("/inbox")["items"]] == ["approval"]


def test_model_usage_is_empty_but_well_formed(tmp_path, monkeypatch):
    with Server(make(tmp_path, monkeypatch).open()) as srv:
        cl = client(srv.url)
        assert cl.get("/models/usage") == {"days": 7, "daily": [], "plugins": []}
        o = cl.get("/models/ollama")
        assert set(o) >= {"reachable", "models"}
