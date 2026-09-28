from __future__ import annotations

import time

from fastapi.testclient import TestClient

from argus import __version__
from argus.api import create_app
from argus.config import load_config
from argus.context import Argus


def make(tmp_path):
    cfg_file = tmp_path / "argus.yaml"
    cfg_file.write_text("logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.05\n", encoding="utf-8")
    return Argus(load_config(cfg_file))


def test_health_and_version(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["version"] == __version__
        assert body["database"]["schema_version"] >= 1
        assert body["watchdog"]["alive"] is True
        assert client.get("/version").json()["version"] == __version__


def test_home_page(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as client:
        r = client.get("/lite")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "/status" in r.text and "/ws/events" in r.text


def test_startup_under_two_seconds(tmp_path):
    t0 = time.perf_counter()
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as client:
        assert client.get("/health").status_code == 200
    assert time.perf_counter() - t0 < 2.0


def test_health_reports_degraded_when_writer_stops(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as client:
        argus.store.close()
        r = client.get("/health")
        assert r.status_code == 503 and r.json()["status"] == "degraded"
