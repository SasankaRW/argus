"""Helios's plugin page: one call with everything, and settings / live changed from Helios (kept across restarts)."""

from __future__ import annotations

import pytest

from argus.worker.client import ApiError
from test_ask import make
from test_worker import Server, client


def test_detail_and_settings(tmp_path, monkeypatch):
    a = make(tmp_path, monkeypatch)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        d = cl.get("/plugins/downloads-organizer")
        assert d["live"] is False and d["buttons"][0]["label"] == "Sort Downloads now"
        assert any(s["cron"] == "30 3 * * *" for s in d["schedules"]) and d["watches"][0]["paths"] == ["~/Downloads"]
        mx = next(s for s in d["settings"] if s["name"] == "max_files_per_run")
        assert (mx["type"], mx["value"], mx["source"]) == ("int", 60, "default")
        assert d["rules"] == "Sorting rules" and d["wrong"] == "Wrong folder"

        r = cl.call("PUT", "/plugins/downloads-organizer/settings",
                    {"live": True, "config": {"max_files_per_run": "25"}})[1]
        assert r["live"] is True and r["config"]["max_files_per_run"] == 25
        d = cl.get("/plugins/downloads-organizer")
        mx = next(s for s in d["settings"] if s["name"] == "max_files_per_run")
        assert (mx["value"], mx["source"], d["live_from"]) == (25, "you", "you")
        assert next(p for p in cl.get("/plugins")["plugins"] if p["id"] == "downloads-organizer")["live"] is True
        with pytest.raises(ApiError) as e:
            cl.call("PUT", "/plugins/downloads-organizer/settings", {"config": {"max_files_per_run": "lots"}})
        assert e.value.status == 422
        with pytest.raises(ApiError):
            cl.call("PUT", "/plugins/downloads-organizer/settings", {"config": {"nope": 1}})
    # a restart keeps your changes
    from argus.config import load_config
    from argus.context import Argus

    b = Argus(load_config(tmp_path / "argus.yaml"))
    with Server(b.open()) as srv:
        cl = client(srv.url)
        d = cl.get("/plugins/downloads-organizer")
        assert d["live"] is True and next(s for s in d["settings"] if s["name"] == "max_files_per_run")["value"] == 25
        r = cl.call("PUT", "/plugins/downloads-organizer/settings", {"reset": True})[1]
        assert r["live"] is False and r["config"]["max_files_per_run"] == 60
