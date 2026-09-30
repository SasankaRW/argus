"""Helios > Settings > "Test Ari's tools": each read-only tool once; nothing that changes things runs."""

from __future__ import annotations

from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.selftest import SAFE, summary
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]


def test_only_tools_that_change_nothing_are_tested():
    changes = {"open_app", "close_app", "type_text", "press_keys", "set_volume", "media_control", "copy_to_clipboard",
               "lock_pc", "show_desktop", "take_screenshot", "run_routine", "save_routine", "delete_routine",
               "add_note", "watch_page", "stop_watching", "run_button", "switch_to_window", "open_file",
               "open_website"}
    assert not changes & set(SAFE)
    assert summary({"notes": [1, 2]}) == "2 notes" and summary({"disks": [1]}) == "1 disk"
    assert summary({"dry_run": True, "x": 1}) == "dry-run"
    assert summary({"text": "my secret", "cut": False}) == "text, cut"  # never the content


def test_the_self_test_runs_each_tool_as_ari_would(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documents" / "notes").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with Server(Argus(load_config(tmp_path / "argus.yaml")).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], watch_folders=False)
        w.register()
        r = cl.post("/tools/selftest", {})

        def done():
            w.run_once(wait=0.3)
            j = cl.get(f"/jobs/{r['job_id']}")
            return j["state"] in ("succeeded", "dead") and j

        j = wait_for(done, timeout=40)
        assert j["state"] == "succeeded", j["error"]
        res = {x["tool"]: x for x in j["result"]["results"]}
        assert res["argus_status"]["ok"] and res["recent_notes"]["ok"] and res["list_routines"]["ok"]
        assert res["list_watches"]["ok"] and res["list_watches"]["about"] == "0 watches"
        assert "look_at_screen" not in res  # only with deep
        assert j["result"]["tested"] == len(res)
        kids = {(x["plugin"], x["workflow"]) for x in cl.get("/jobs?limit=200")}
        assert {p for p, _ in kids} & {"pc-keys", "pc-media"} == set()
        assert {wf for p, wf in kids if p == "pc-apps"} <= {"list_apps"}
