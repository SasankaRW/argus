"""C10: plugins as folders (manifest, worker-side ctx limited to the manifest) and the simulated power manager.
Gate: a test plugin runs from its folder; "would shut down" is logged after idle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.config import Config, PowerConfig, load_config
from argus.context import Argus
from argus.plugins import PluginHost
from argus.power import PowerManager
from argus.worker import Worker
from argus.worker.plugins import Files, PermissionDenied, Secrets
from conftest import run
from test_worker import Server, client, wait_for

PLUGIN_PY = '''
from argus.worker import workflow


@workflow("tidy", "sort")
def sort(ctx):
    mode = ctx.input.get("mode")
    if mode == "escape":
        ctx.files.read_text(ctx.input["path"])
    if mode == "net":
        ctx.http.get_json("https://evil.example/x")
    if mode == "secret":
        ctx.secrets["OTHER_TOKEN"]
    if mode == "models":
        ctx.llm("say hi", "x")
    moved = [ctx.step("move", ctx.files.move, f, ctx.config["out"]) for f in ctx.files.list(ctx.config["inbox"])]
    ctx.emit("sorted", count=len(moved))
    ctx.store.set("runs", (ctx.store.get("runs", 0) or 0) + 1)
    return {"moved": moved, "dry_run": ctx.dry_run, "key": ctx.secrets.get("TIDY_KEY")}
'''


def manifest(inbox: Path, out: Path, **over) -> str:
    m = {"id": "tidy", "name": "Tidy", "version": "0.1.0", "kind": "workflow", "argus_api": ">=1.0 <2.0",
         "runs_on": "any", "triggers": [{"manual": {"workflow": "sort", "label": "Sort now"}}],
         "permissions": {"files": {"read": [str(inbox)], "write": [str(inbox), str(out)]},
                         "network": ["api.example"], "secrets": ["TIDY_KEY"]},
         "config": {"inbox": {"type": "path", "default": str(inbox)}, "out": {"type": "path", "default": str(out)}}}
    m.update(over)
    return json.dumps(m)  # JSON is YAML


def setup(tmp_path: Path, live: bool = False, **over) -> tuple[Argus, Path, Path]:
    inbox, out = tmp_path / "inbox", tmp_path / "out"
    inbox.mkdir()
    out.mkdir()
    (inbox / "a.txt").write_text("a")
    (inbox / "b.txt").write_text("b")
    (out / "a.txt").write_text("already here")  # a move must never overwrite this
    folder = tmp_path / "plugins" / "tidy"
    folder.mkdir(parents=True)
    (folder / "plugin.yaml").write_text(manifest(inbox, out, **over))
    (folder / "plugin.py").write_text(PLUGIN_PY)
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(tmp_path / 'plugins').as_posix()}']\n  live: {['tidy'] if live else []}\n")
    (tmp_path / ".env").write_text("TIDY_KEY=k-123\nOTHER_TOKEN=nope\n")
    return Argus(load_config(tmp_path / "argus.yaml")), inbox, out


def run_job(tmp_path: Path, argus: Argus, body: dict, monkeypatch) -> dict:
    monkeypatch.chdir(tmp_path)  # ctx.secrets reads ./.env, like a worker started from the repo
    with Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "w-plugins", watch_folders=False)
        w.register()
        assert "tidy" in w.plugins, w.plugin_errors
        job = cl.post("/jobs", {"plugin": "tidy", "workflow": "sort", **body})
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead", "retry")
                        and j)
        done["events"] = cl.get("/events?kinds=file.,plugin.&limit=100")["events"]
        done["state_runs"] = cl.get("/plugins/tidy/state/runs")["value"]
        done["plugins"] = cl.get("/plugins")
        return done


def test_plugin_runs_from_its_folder_in_dry_run(tmp_path, monkeypatch):
    argus, inbox, out = setup(tmp_path)
    job = run_job(tmp_path, argus, {"input": {}}, monkeypatch)
    assert job["state"] == "succeeded", job
    res = job["result"]
    assert res["dry_run"] is True and res["key"] == "k-123"
    assert sorted(Path(p).name for p in res["moved"]) == ["a (1).txt", "b.txt"]  # never overwrites
    assert sorted(p.name for p in inbox.iterdir()) == ["a.txt", "b.txt"]  # dry run: nothing moved
    kinds = [e["kind"] for e in job["events"]]
    assert kinds.count("file.moved") == 2 and "plugin.sorted" in kinds
    assert all(e["data"]["dry_run"] for e in job["events"] if e["kind"] == "file.moved")
    assert job["state_runs"] == 1
    assert [p["id"] for p in job["plugins"]["plugins"]] == ["tidy"] and job["plugins"]["errors"] == []


def test_live_plugin_changes_files(tmp_path, monkeypatch):
    argus, inbox, out = setup(tmp_path, live=True)
    job = run_job(tmp_path, argus, {"input": {}}, monkeypatch)
    assert job["state"] == "succeeded" and job["result"]["dry_run"] is False
    assert list(inbox.iterdir()) == []
    assert sorted(p.name for p in out.iterdir()) == ["a (1).txt", "a.txt", "b.txt"]
    assert (out / "a.txt").read_text() == "already here"


@pytest.mark.parametrize("mode,words", [("escape", "may not read"), ("net", "may not call"),
                                        ("secret", "may not read secret"), ("models", "may not use these models")])
def test_permission_denials_kill_the_job(tmp_path, monkeypatch, mode, words):
    argus, inbox, _ = setup(tmp_path)
    job = run_job(tmp_path, argus, {"input": {"mode": mode, "path": str(tmp_path / ".env")}}, monkeypatch)
    assert job["state"] == "dead" and words in job["error"], job
    assert sorted(p.name for p in inbox.iterdir()) == ["a.txt", "b.txt"]


def test_bad_plugins_are_listed_not_fatal(tmp_path):
    argus, _, _ = setup(tmp_path)
    root = tmp_path / "plugins"
    for name, body, code in [
        ("wrong-name", manifest(tmp_path, tmp_path, id="other"), True),
        ("no-code", manifest(tmp_path, tmp_path, id="no-code"), False),
        ("old-api", manifest(tmp_path, tmp_path, id="old-api", argus_api=">=2.0"), True),
        ("bad-yaml", "id: [", True),
    ]:
        (root / name).mkdir()
        (root / name / "plugin.yaml").write_text(body)
        if code:
            (root / name / "plugin.py").write_text("")
    host = PluginHost(argus.cfg)
    host.load()
    listed = host.list()
    assert [p["id"] for p in listed["plugins"]] == ["tidy"]
    errs = {Path(e["folder"]).name: e["error"] for e in listed["errors"]}
    assert set(errs) == {"wrong-name", "no-code", "old-api", "bad-yaml"}
    assert "folder name" in errs["wrong-name"] and "plugin.py" in errs["no-code"] and "argus_api" in errs["old-api"]


def test_desktop_plugins_only_go_to_desktop_workers(tmp_path):
    argus, _, _ = setup(tmp_path, runs_on="desktop")
    host = PluginHost(argus.cfg)
    host.load()
    assert host.for_worker(["laptop"]) == []
    assert [p["id"] for p in host.for_worker(["desktop", "gpu"])] == ["tidy"]
    assert host.needs_for("tidy") == ["desktop"]


def test_files_rules(tmp_path):
    ok, other, blocked = tmp_path / "ok", tmp_path / "other", tmp_path / "ok" / "private"
    for d in (ok, other, blocked):
        d.mkdir(parents=True, exist_ok=True)
    (ok / "x.txt").write_text("x")
    (blocked / "s.txt").write_text("s")
    trace = []
    f = Files("p", {"read": [str(ok)], "write": [str(ok)]}, {"blocked": [str(blocked)]}, False,
              lambda k, d: trace.append(k))
    assert f.read_text(ok / "x.txt") == "x"
    with pytest.raises(PermissionDenied):
        f.read_text(ok / ".." / "other" / "x.txt")  # .. can't step outside
    with pytest.raises(PermissionDenied):
        f.read_text(blocked / "s.txt")
    with pytest.raises(PermissionDenied):
        f.recycle(ok / "x.txt")  # delete: none
    f2 = Files("p", {"write": [str(ok)], "delete": "recycle_bin"}, {"allowed": [str(tmp_path)]}, False,
               lambda k, d: trace.append(k))
    f2.recycle(ok / "x.txt")
    assert not (ok / "x.txt").exists() and trace[-1] == "file.recycled"
    f3 = Files("p", {"write": [str(other)]}, {"allowed": [str(ok)]}, False, lambda k, d: None)
    with pytest.raises(PermissionDenied, match="paths.allowed"):
        f3.write_text(other / "n.txt", "n")


def test_secrets_only_declared(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("A=1\nB=2\n")
    s = Secrets("p", ["A"])
    assert s["A"] == "1"
    with pytest.raises(PermissionDenied):
        s["B"]


# ------------------------------------------------------------------ power (simulated)


def test_power_would_wake_then_would_shut_down_after_idle(store, jobs, clock):
    cfg = Config(power=PowerConfig(idle_minutes=20))
    pm = PowerManager(store, cfg, clock=clock)
    run(pm.start())
    job_id, _ = run(jobs.enqueue("tidy", "sort", needs=["gpu"]))
    assert run(pm.tick()) == "busy"  # GPU work waiting and no PC online
    clock.advance(60)
    run(pm.tick())  # said once, not again within 10 minutes
    run(jobs.cancel(job_id))
    assert run(pm.tick()) == "idle"
    clock.advance(19 * 60)
    assert run(pm.tick()) == "idle"
    clock.advance(61)
    assert run(pm.tick()) == "would_shutdown"
    clock.advance(3600)
    assert run(pm.tick()) == "would_shutdown"  # once per idle stretch

    def kinds(conn):
        return [r[0] for r in conn.execute("SELECT kind FROM events WHERE kind LIKE 'power.%' ORDER BY rowid")]

    assert run(store.read(kinds)) == ["power.would_wake", "power.would_shutdown"]
    comp = run(store.read(lambda c: c.execute("SELECT meta FROM components WHERE id = 'power'").fetchone()[0]))
    assert json.loads(comp)["state"] == "would_shutdown"
    # new PC work starts a new stretch
    run(jobs.enqueue("tidy", "sort", needs=["desktop"]))
    assert run(pm.tick()) == "busy" and pm.idle_since is None
