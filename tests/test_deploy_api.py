from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from argus.api import create_app
from argus.config import load_config
from argus.context import Argus

H = {"Authorization": "Bearer t0ken"}


def make(tmp_path):
    cfg_file = tmp_path / "argus.yaml"
    cfg_file.write_text("logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.05\n", encoding="utf-8")
    cfg = load_config(cfg_file)
    cfg.secrets.worker_token = "t0ken"
    return Argus(cfg).open()


def test_guarded(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        assert c.get("/deploy").status_code == 401
        assert "commit_subject" not in c.get("/version").json()  # /version stays as it was, and public


def test_no_files_means_stale_and_nulls(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        body = c.get("/deploy", headers=H).json()
        assert body["last_check"] is None and body["last_update"] is None and body["stale"] is True
        assert body["started_at"] <= time.time()
        assert {"commit", "commit_subject"} <= body.keys()


def test_reads_the_data_dir_and_stale_logic(tmp_path):
    argus = make(tmp_path)
    data = argus.cfg.db_path.parent
    data.mkdir(parents=True, exist_ok=True)
    upd = {"at": 1, "from": "a" * 40, "to": "b" * 40, "subject": "fix: x", "result": "rolled_back",
           "step": "pip", "detail": "pip failed"}
    (data / "last-update.json").write_text(json.dumps(upd), encoding="utf-8")
    chk = {"checked_at": int(time.time()) - 60, "origin": "b" * 40, "behind": 1}
    (data / "last-check.json").write_text(json.dumps(chk), encoding="utf-8")
    with TestClient(create_app(argus)) as c:
        body = c.get("/deploy", headers=H).json()
        assert body["last_update"] == upd and body["last_check"] == chk and body["stale"] is False
        chk["checked_at"] = int(time.time()) - 16 * 60  # the updater went quiet
        (data / "last-check.json").write_text(json.dumps(chk), encoding="utf-8")
        assert c.get("/deploy", headers=H).json()["stale"] is True
        (data / "last-check.json").write_text("{half a file", encoding="utf-8")  # unreadable = missing
        body = c.get("/deploy", headers=H).json()
        assert body["last_check"] is None and body["stale"] is True
