"""The phone plugin: no plugin.py (the Android app runs it), jobs only go to a worker with `phone`."""

from __future__ import annotations

from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from test_worker import Server, client

ROOT = Path(__file__).resolve().parents[1]


def test_phone_jobs_wait_for_the_phone(tmp_path):
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n", encoding="utf-8")
    with Server(Argus(load_config(tmp_path / "argus.yaml")).open()) as srv:
        cl = client(srv.url)
        info = {p["id"]: p for p in cl.get("/plugins")["plugins"]}
        assert info["phone"]["runs_on"] == "phone"
        tools = {t["name"]: t for t in cl.get("/tools")}
        assert tools["phone_status"]["plugin"] == "phone"
        pc = cl.post("/workers/register", {"id": "worker-pc", "host": "pc", "capabilities": ["desktop", "gpu"]})
        assert "phone" not in {p["id"] for p in pc["plugins"]}
        job = cl.post("/jobs", {"plugin": "phone", "workflow": "status"})
        assert cl.call("POST", "/workers/worker-pc/claim", {"capabilities": ["desktop", "gpu"], "wait": 0})[0] == 204
        ph = cl.post("/workers/register", {"id": "phone-pixel", "host": "pixel", "capabilities": ["phone"]})
        assert "phone" in {p["id"] for p in ph["plugins"]}
        got = cl.post("/workers/phone-pixel/claim", {"capabilities": ["phone"], "wait": 0})
        assert got["id"] == job["id"] and got["plugin_info"]["live"] is False
