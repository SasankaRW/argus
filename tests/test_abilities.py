"""routines, notes, homelab, devhelp: the helpers, then a real Argus + worker running them."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from argus.config import load_config
from argus.context import Argus
from argus.worker import PermanentError, Worker
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"t_{name.replace('-', '_')}", ROOT / "plugins" / name / "plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


routines = load("routines")
homelab = load("homelab")
devhelp = load("devhelp")


def test_routine_steps_in_plain_text():
    steps = routines.parse_steps("open_app name=Visual Studio Code; open_app name=Google Chrome\nset_volume level=20")
    assert steps == [{"tool": "open_app", "args": {"name": "Visual Studio Code"}},
                     {"tool": "open_app", "args": {"name": "Google Chrome"}},
                     {"tool": "set_volume", "args": {"level": 20}}]
    assert routines.show(steps) == "open_app name=Visual Studio Code; open_app name=Google Chrome; set_volume level=20"
    assert routines.parse_steps("- show_desktop") == [{"tool": "show_desktop", "args": {}}]
    with pytest.raises(PermanentError, match="needs a name"):
        routines.parse_steps("open_app Chrome")
    with pytest.raises(PermanentError, match="can't run or change routines"):
        routines.parse_steps("run_routine name=work mode")
    with pytest.raises(PermanentError, match="at least one step"):
        routines.parse_steps(" ; ")


def test_lab_readings_and_what_needs_attention():
    gpu = homelab.parse_gpu("NVIDIA GeForce RTX 5070, 12, 3072, 12288, 88\n")
    assert gpu == [{"name": "NVIDIA GeForce RTX 5070", "busy_percent": 12.0, "memory_used_gb": 3.0,
                    "memory_total_gb": 12.0, "temperature_c": 88.0}]
    ts = homelab.parse_tailscale(json.dumps({
        "BackendState": "Running", "Self": {"HostName": "SasPC"},
        "Peer": {"a": {"HostName": "pixel-8a", "Online": True}, "b": {"HostName": "old-laptop", "Online": False}}}))
    assert ts == {"this_device": "SasPC", "running": True, "online": ["pixel-8a"], "offline": ["old-laptop"]}
    r = {"disks": [{"drive": "C:\\", "free_gb": 20.0, "total_gb": 500.0, "free_percent": 4.0},
                   {"drive": "G:\\", "free_gb": 900.0, "total_gb": 1000.0, "free_percent": 90.0}],
         "gpu": gpu, "backup": {"enabled": True, "hours_ago": 70.2}, "tailscale": ts}
    assert [k for k, _ in homelab.warnings(r, {})] == ["disk:C:\\", "gpu:hot", "backup:old"]
    assert homelab.warnings({"backup": {"enabled": True, "hours_ago": 3}}, {}) == []
    assert homelab.parse_gpu("garbage") is None and homelab.parse_tailscale("{bad") is None


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(folder: Path, name: str) -> Path:
    r = folder / name
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "first commit")
    return r


def test_repo_status_from_git(tmp_path):
    repo = make_repo(tmp_path / "Projects", "argus")
    (repo / "b.txt").write_text("new")
    (tmp_path / "Projects" / "not-a-repo").mkdir()
    assert devhelp.find_repos([str(tmp_path / "Projects")]) == [str(repo)]
    info = devhelp.repo_info(str(repo), with_ci=False)
    assert info["repo"] == "argus" and info["branch"] == "main" and info["uncommitted"] == 1
    assert info["uncommitted_files"] == ["b.txt"] and info["last_commit"]["message"] == "first commit"
    assert info["upstream"] == "none (not pushed yet)"
    assert devhelp.parse_ahead_behind("1\t3") == (3, 1) and devhelp.parse_ahead_behind("") is None


def setup(tmp_path: Path, monkeypatch) -> tuple[Argus, Path]:
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    projects = tmp_path / "Projects"
    make_repo(projects, "argus")
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "models:\n  tiers:\n    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n  chain: [T1]\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: [routines, notes, devhelp]\n"
        f"  config:\n    devhelp: {{folders: ['{projects.as_posix()}'], ci: false}}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml")), home


def call(cl, w, plugin: str, workflow: str, **inp):
    job = cl.post("/jobs", {"plugin": plugin, "workflow": workflow, "needs": ["desktop"], "input": inp})

    def done():
        w.run_once(wait=0.5)  # a routine's steps are jobs of their own: keep working until this one ends
        j = cl.get(f"/jobs/{job['id']}")
        return j["state"] in ("succeeded", "dead") and j

    j = wait_for(done, timeout=20)
    assert j["state"] == "succeeded", j["error"]
    return j["result"]


def test_notes_routines_and_repos_for_real(tmp_path, monkeypatch):
    argus, home = setup(tmp_path, monkeypatch)
    with Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], watch_folders=False)
        w.register()
        assert {"routines", "notes", "devhelp"} <= set(w.plugins), w.plugin_errors

        # notes: one file a day, only added to
        assert call(cl, w, "notes", "add", text="note: buy printer ink")["added"].endswith("buy printer ink")
        call(cl, w, "notes", "add", text="call the bank about the card")
        files = list((home / "Documents/notes").glob("*.md"))
        assert len(files) == 1
        body = files[0].read_text()
        assert body.startswith("# Notes ") and body.count("\n- ") == 2 and "note:" not in body
        found = call(cl, w, "notes", "find", query="printer")["notes"]
        assert [n["note"] for n in found] == ["buy printer ink"]
        assert [n["note"] for n in call(cl, w, "notes", "recent")["notes"]] == ["call the bank about the card",
                                                                               "buy printer ink"]

        # routines: the default is there; a saved one runs its steps in order (a plugin tool, then a built-in)
        assert [r["name"] for r in call(cl, w, "routines", "list")["routines"]] == ["work mode"]
        call(cl, w, "routines", "save", name="Evening", steps="add_note text=evening routine ran; argus_status")
        r = call(cl, w, "routines", "run", name="evening")
        assert r["routine"] == "Evening" and [d["step"] for d in r["done"]] == ["1. add_note", "2. argus_status"]
        assert "evening routine ran" in files[0].read_text()
        bad = call(cl, w, "routines", "save", name="broken", steps="no_such_tool; argus_status")
        assert bad["saved"] == "broken"
        stopped = call(cl, w, "routines", "run", name="broken")
        assert stopped["stopped_at"] == "1. no_such_tool" and stopped["done"] == []
        assert call(cl, w, "routines", "delete", name="broken")["deleted"] == "broken"
        assert sorted(x["name"] for x in call(cl, w, "routines", "list")["routines"]) == ["Evening", "work mode"]

        # devhelp: the repo and what changed today
        repos = call(cl, w, "devhelp", "repos")["repos"]
        assert [x["repo"] for x in repos] == ["argus"] and repos[0]["uncommitted"] == 0
        today = call(cl, w, "devhelp", "today")["today"]
        assert today and today[0]["commits"][0]["message"] == "first commit"

        tools = {t["name"]: t for t in cl.get("/tools")}
        assert tools["save_routine"]["risky"] and not tools["run_routine"]["risky"] and not tools["add_note"]["risky"]
        assert {"lab_status", "repo_status", "what_changed_today", "find_notes", "backup_status"} <= set(tools)
