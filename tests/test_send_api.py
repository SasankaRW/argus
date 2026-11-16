from __future__ import annotations

import time

from fastapi.testclient import TestClient

from argus.api import create_app
from argus.config import load_config
from argus.context import Argus

KEY = "A" * 43 + "="


def make(tmp_path, token=True):
    cfg_file = tmp_path / "argus.yaml"
    cfg_file.write_text("logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.05\n", encoding="utf-8")
    cfg = load_config(cfg_file)
    if token:
        cfg.secrets.worker_token = "t0ken"
    return Argus(cfg).open()


def test_devices_events_history(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        h = {"Authorization": "Bearer t0ken"}
        assert c.get("/send/devices").status_code == 401  # worker-token guarded
        assert c.post("/send/events", json={}).status_code == 401
        dev = {"id": "pc", "name": "PC", "pubkey": KEY, "lan": ["192.0.2.1:8610"], "tailscale": "pc.example.ts.net"}
        assert c.post("/send/devices", json=dev, headers=h).json() == {"ok": True, "key_changed": False}
        assert c.post("/send/devices", json={**dev, "pubkey": "B" * 43 + "="}, headers=h).json()["key_changed"]
        got = c.get("/send/devices", headers=h).json()
        assert got[0]["id"] == "pc" and got[0]["lan"] == ["192.0.2.1:8610"] and got[0]["pubkey"] == "B" * 43 + "="
        ev = {"eid": "e1" * 8, "device": "pc", "kind": "sent", "peer": "Calypso", "label": "a.txt", "bytes": 5,
              "at": time.time() - 100, "offer_id": "o" * 8}
        assert c.post("/send/events", json=ev, headers=h).json()["stored"] is True
        assert c.post("/send/events", json=ev, headers=h).json()["stored"] is False  # a re-sent event, stored once
        c.post("/send/events", json={**ev, "eid": "e2" * 8, "kind": "received", "at": time.time() - 50}, headers=h)
        rows = c.get("/send/history?limit=5", headers=h).json()
        assert [r["kind"] for r in rows] == ["received", "sent"] and rows[1]["bytes"] == 5
        assert c.post("/send/devices", json={**dev, "id": "Bad Id"}, headers=h).status_code == 422
