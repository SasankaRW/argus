"""Reading logs for Helios and `dev.ps1 logs`: JSON lines, Ollama lines, plain tracebacks; following a file."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from argus import logview
from argus.api import create_app
from argus.config import load_config
from argus.context import Argus


def test_parse_the_three_kinds():
    j = logview.parse(json.dumps({"ts": "2026-09-29T10:00:00.000Z", "level": "info", "logger": "argus.worker",
                                  "msg": "job started", "job": "01ABC"}), "worker")
    assert j["msg"] == "job started" and j["extra"] == {"job": "01ABC"} and j["level"] == "info"
    line = 'time=2026-09-29T10:00:01.000+05:30 level=WARN source=server.go:12 msg="gpu low" free=1.2'
    o = logview.parse(line, "ollama")
    assert o["level"] == "warn" and o["msg"] == "gpu low" and o["extra"] == {"free": "1.2"}
    t = logview.parse("Traceback (most recent call last):", "argusd-crash")
    assert t["level"] == "error" and logview.parse("   ", "x") is None


def test_read_tail_then_follow_without_half_lines(tmp_path):
    f = tmp_path / "worker.log"
    f.write_text("".join(json.dumps({"level": "info", "msg": f"line {i}"}) + "\n" for i in range(10)))
    r = logview.read(f, "worker", lines=3)
    assert [e["msg"] for e in r["entries"]] == ["line 7", "line 8", "line 9"]
    with open(f, "a") as fh:
        fh.write(json.dumps({"level": "error", "msg": "boom"}) + "\n" + '{"level": "info", "msg": "hal')
    r2 = logview.read(f, "worker", after=r["offset"])
    assert [e["msg"] for e in r2["entries"]] == ["boom"]  # the half-written line waits
    with open(f, "a") as fh:
        fh.write('f"}\n')
    assert [e["msg"] for e in logview.read(f, "worker", after=r2["offset"])["entries"]] == ["half"]
    f.write_text(json.dumps({"msg": "rotated"}) + "\n")  # smaller than the offset: start again
    assert [e["msg"] for e in logview.read(f, "worker", after=r2["offset"] + 50)["entries"]] == ["rotated"]


def test_logs_api_lists_and_reads(tmp_path):
    (tmp_path / "argus.yaml").write_text("logging:\n  file: logs/argus.log\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "worker.log").write_text(json.dumps({"level": "info", "msg": "hello"}) + "\n")
    (logs / "argusd-crash.log").write_text("")  # empty: not listed
    (logs / "notes.txt").write_text("x")
    argus = Argus(load_config(tmp_path / "argus.yaml")).open()
    with TestClient(create_app(argus)) as c:
        names = {s["name"] for s in c.get("/logs").json()}
        assert "worker" in names and "argusd-crash" not in names and "notes" not in names
        assert c.get("/logs/worker").json()["entries"][0]["msg"] == "hello"
        assert c.get("/logs/..%2f.env").status_code == 404
        assert c.get("/logs/nope").status_code == 404


def test_a_log_it_cant_read_never_breaks_the_list(tmp_path, monkeypatch):
    """On the laptop the service can't look into /home: /logs crashed on the Ollama app's log path every time."""
    from pathlib import Path

    from argus import logview

    (tmp_path / "argus.log").write_text('{"ts": "x", "level": "info", "msg": "hi"}\n')
    (tmp_path / "worker-crash.log").write_text("")  # an empty crash log is left out
    monkeypatch.setattr(logview, "WINDOWS", False)
    assert list(logview.sources(tmp_path)) == ["argus"]  # not looked for off Windows
    monkeypatch.setattr(logview, "WINDOWS", True)
    real = Path.is_file

    def is_file(self):
        if "Ollama" in str(self):
            raise PermissionError(13, "Permission denied")
        return real(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert list(logview.sources(tmp_path)) == ["argus"]
