"""Everything search: es.exe's CSV read into files, blocked folders left out, clear errors."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

from argus.worker.plugins import Files

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t_everything", ROOT / "plugins" / "everything" / "plugin.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)

OUT = ('Filename,Size,Date Modified\r\n"C:\\Users\\sas\\Documents\\cv 2026.pdf",204800,2026-09-30 21:04\r\n'
       '"D:\\DirectFN\\client report.xlsx",1024,2026-09-29 10:00\r\n')


def test_parse_and_blocked_folders(tmp_path):
    files = ev.parse(OUT)
    assert files[0] == {"path": "C:\\Users\\sas\\Documents\\cv 2026.pdf", "name": "cv 2026.pdf", "size_kb": 200,
                        "modified": "2026-09-30 21:04"}
    assert ev.parse("") == []
    blocked = tmp_path / "office"
    f = Files("everything", {}, {"blocked": [str(blocked)]}, False, lambda *a: None)
    assert f.visible(tmp_path / "home" / "cv.pdf") and not f.visible(blocked / "report.xlsx")
    assert ev.args("es.exe", "ext:pdf dm:today", 5)[-1] == "ext:pdf dm:today"  # one argument: no shell


def test_search_workflow(tmp_path, monkeypatch):
    class Ctx:
        input = {"query": "cv"}
        config = {"results": 1, "es_path": ""}
        files = Files("everything", {}, {}, False, lambda *a: None)

        def step(self, _name, fn):
            return fn()

    monkeypatch.setattr(ev, "WIN", True)
    monkeypatch.setattr(ev, "find_es", lambda c: "es.exe")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, OUT, ""))
    r = ev.search(Ctx())
    assert r["files"][0]["name"] == "cv 2026.pdf" and r["more"] == 1
