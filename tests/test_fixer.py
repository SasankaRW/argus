"""The ticket fixer's plan phase: Tracker (fake) -> Claude in the project folder (fake, read-only) -> plan attached
to the ticket -> your approval -> label fix-approved. Plus the pure rules (models, labels, plan checks)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixer_rules", ROOT / "plugins" / "fixer" / "plugin.py")
fx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fx)

PLAN = "\n".join(f"## {s}\n{'Details about it. ' * 4}" for s in fx.SECTIONS)


def test_projects_and_models():
    got, bad = fx.parse_projects(["trk | G:\\Projects\\tracker | npm run build | claude:Opus | haiku", "# note", "",
                                  "ACME | C:\\acme", "bad line", "X | y | z | gpt-4"])
    assert got["TRK"] == {"key": "TRK", "path": "G:\\Projects\\tracker", "test": "npm run build",
                          "plan_model": "opus", "fix_model": "haiku"}
    assert got["ACME"]["test"] == "" and len(bad) == 2 and "gpt-4" in bad[1]
    project, cfg = got["TRK"], {"plan_model": "sonnet", "fix_model": "sonnet"}
    assert fx.pick_models({}, [], got["ACME"], cfg) == ("sonnet", "sonnet")  # defaults
    assert fx.pick_models({}, [], project, cfg) == ("opus", "haiku")  # the project line
    assert fx.pick_models({}, ["plan:sonnet"], project, cfg) == ("sonnet", "haiku")  # ticket labels
    assert fx.pick_models({"plan_model": "haiku", "fix_model": "Opus"}, ["plan:sonnet"], project, cfg) == \
        ("haiku", "opus")  # what you said to Ari
    with pytest.raises(fx.PermanentError):
        fx.pick_models({"plan_model": "gpt-4"}, [], project, cfg)
    assert fx.norm_model("claude-opus-4-1") == "claude-opus-4-1"


def test_which_tickets_get_planned():
    projects = {"TRK": {}}
    t = lambda key, labels, status="todo": {"key": key, "labels": labels, "status": status}  # noqa: E731
    got = fx.eligible([t("TRK-1", ["ai-fix"]), t("TRK-2", ["ai-fix", "plan-ready"]), t("TRK-3", ["bug"]),
                       t("OTHER-4", ["ai-fix"]), t("TRK-5", ["AI-FIX"], "done"), t("TRK-6", ["ai-fix", "ai-planning"])],
                      projects)
    assert [x["key"] for x in got] == ["TRK-1"]
    assert fx.stage(["ai-fix"]) == "waiting for a plan" and fx.stage(["ai-fix", "plan-ready"]).startswith("plan ready")
    assert fx.stage(["bug"]) == ""


def test_plan_checks_and_the_claude_command_is_read_only():
    assert fx.plan_problem(PLAN) is None
    assert "missing sections" in fx.plan_problem(PLAN.replace("## Risks", "## Notes"))
    assert "too short" in fx.plan_problem("fix it")
    assert fx.section(PLAN, "Steps").startswith("Details")
    args = fx.claude_args("claude", "opus")
    deny = args[args.index("--disallowedTools") + 1].split(",")
    assert {"Bash", "Edit", "Write", "WebFetch", "WebSearch"} <= set(deny) and "Read(**/.env*)" in deny
    assert args[args.index("--allowedTools") + 1] == "Read,Grep,Glob" and "plan" in args
    assert fx.claude_text(json.dumps({"result": "the plan"})) == "the plan"
    with pytest.raises(fx.PlanError):
        fx.claude_text(json.dumps({"is_error": True, "result": "rate limited"}))
    body, ctype = fx.multipart("a.md", b"hello")
    assert b'filename="a.md"' in body and b"hello" in body and ctype.startswith("multipart/form-data; boundary=")


class Tracker:
    def __init__(self, labels):
        self.ticket = {"id": 5, "key": "TRK-5", "title": "Board drops cards", "status": "todo", "priority": 1,
                       "type": "bug", "labels": labels, "description": "Drag a card, it vanishes.",
                       "checklist": [], "due_date": None}
        self.other = {"id": 9, "key": "OTH-1", "title": "Other", "status": "todo", "labels": ["ai-fix"]}
        self.comments: list[str] = []
        self.files: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        me = self

        class H(BaseHTTPRequestHandler):
            def go(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                me.calls.append((method, self.path))
                if self.headers.get("X-API-Key") != "k":
                    return self.send(401, {})
                p = self.path
                if p == "/api/tickets":
                    return self.send(200, [me.ticket, me.other])
                if p == "/api/tickets/5" and method == "GET":
                    return self.send(200, me.ticket)
                if p == "/api/tickets/5" and method == "PATCH":
                    me.ticket.update(json.loads(raw))
                    return self.send(200, me.ticket)
                if p == "/api/tickets/5/comments":
                    if method == "POST":
                        me.comments.append(json.loads(raw)["body"])
                        return self.send(200, {"id": 1, "body": "x", "created_at": "2026-10-03T10:00:00"})
                    return self.send(200, [{"body": "seen on staging", "created_at": "2026-10-02T09:00:00"}])
                if p == "/api/tickets/5/attachments":
                    if method == "POST":
                        name = re.search(rb'filename="([^"]+)"', raw).group(1).decode()
                        me.files[name] = raw.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0].decode()
                        return self.send(200, {"id": 1, "filename": name})
                    return self.send(200, [{"filename": "screen.png"}])
                return self.send(404, {})

            def send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            do_GET = lambda self: self.go("GET")  # noqa: E731
            do_POST = lambda self: self.go("POST")  # noqa: E731
            do_PATCH = lambda self: self.go("PATCH")  # noqa: E731

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


def start(tmp_path, monkeypatch, labels, claude):
    tr = Tracker(labels)
    project = tmp_path / "trk"
    project.mkdir()
    (tmp_path / ".env").write_text("TRACKER_API_KEY=k\n", encoding="utf-8")
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: [fixer]\n  config:\n    fixer:\n"
        f"      tracker_url: '{tr.url}'\n      claude_command: fakeclaude\n      plan_model: opus\n"
        f"      projects:\n        - 'TRK | {project.as_posix()} | npm run build | opus | sonnet'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    srv = Server(Argus(load_config(tmp_path / "argus.yaml")).open())
    srv.__enter__()
    cl = client(srv.url)
    w = Worker(cl, "pc", capabilities=["desktop"], watch_folders=False)
    w.register()
    assert "fixer" in w.plugins, w.plugin_errors
    monkeypatch.setattr(sys.modules["argus_plugin_fixer"], "run_claude", claude)
    return tr, cl, w, srv, project


def test_plan_attached_then_approved(tmp_path, monkeypatch):
    seen: list[dict] = []
    answers = ["## Summary\nshort", PLAN]  # the first answer is refused, the second is used

    def claude(args, prompt, cwd, timeout):
        seen.append({"args": args, "prompt": prompt, "cwd": cwd, "timeout": timeout})
        return answers[len(seen) - 1]

    tr, cl, w, srv, project = start(tmp_path, monkeypatch, ["ai-fix", "plan:haiku"], claude)
    try:
        job = cl.post("/jobs", {"plugin": "fixer", "workflow": "plan", "needs": ["desktop"],
                                "input": {"key": "trk-5"}})
        assert w.run_once(wait=2)
        wait_for(lambda: cl.get(f"/jobs/{job['id']}")["state"] == "waiting")
        assert len(seen) == 2 and "refused" in seen[1]["prompt"] and "too short" in seen[1]["prompt"]
        assert seen[0]["cwd"] == project.as_posix() and seen[0]["timeout"] == 600
        assert seen[0]["args"][seen[0]["args"].index("--model") + 1] == "haiku"  # the ticket's label
        assert "Board drops cards" in seen[0]["prompt"] and "seen on staging" in seen[0]["prompt"]
        plan_file = tr.files["TRK-5-fix-plan.md"]
        assert plan_file.count("## Summary") == 1 and "fix model: sonnet" in plan_file
        assert "plan-ready" in tr.ticket["labels"] and "ai-planning" not in tr.ticket["labels"]
        assert tr.comments[0].startswith("Fix plan ready (plan: haiku, fix: sonnet)")
        a = cl.get("/approvals?state=pending")[0]
        assert a["title"] == "Run the fix for TRK-5 with sonnet?"
        cl.post(f"/approvals/{a['id']}/decide", {"answer": "approve"})
        wait_for(lambda: cl.get(f"/jobs/{job['id']}")["state"] == "queued")
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)
        assert done["state"] == "succeeded", done["error"]
        assert done["result"]["approved"] is True and done["result"]["plan_model"] == "haiku"
        assert "fix-approved" in tr.ticket["labels"] and "plan-ready" not in tr.ticket["labels"]
        assert "ai-fix" in tr.ticket["labels"] and len(seen) == 2  # not planned again after the approval
        assert len(tr.files) == 1
    finally:
        srv.__exit__(None, None, None)


def test_rejected_and_failed_plans_say_so_on_the_ticket(tmp_path, monkeypatch):
    def claude(args, prompt, cwd, timeout):
        return PLAN

    tr, cl, w, srv, _ = start(tmp_path, monkeypatch, ["ai-fix"], claude)
    try:
        job = cl.post("/jobs", {"plugin": "fixer", "workflow": "plan", "needs": ["desktop"], "input": {"key": "TRK-5"}})
        assert w.run_once(wait=2)
        wait_for(lambda: cl.get(f"/jobs/{job['id']}")["state"] == "waiting")
        cl.post(f"/approvals/{cl.get('/approvals?state=pending')[0]['id']}/decide", {"answer": "reject"})
        wait_for(lambda: cl.get(f"/jobs/{job['id']}")["state"] == "queued")
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)
        assert done["result"]["approved"] is False and "plan-rejected" in tr.ticket["labels"]
        assert tr.comments[-1].startswith("Fix not approved")

        def broken(args, prompt, cwd, timeout):
            raise sys.modules["argus_plugin_fixer"].PlanError("the plan took longer than 10 minutes and was stopped")

        monkeypatch.setattr(sys.modules["argus_plugin_fixer"], "run_claude", broken)
        job = cl.post("/jobs", {"plugin": "fixer", "workflow": "plan", "needs": ["desktop"], "input": {"key": "TRK-5"}})
        assert w.run_once(wait=2)
        dead = wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)
        assert dead["state"] == "dead" and "took longer" in dead["error"]
        assert "plan-failed" in tr.ticket["labels"] and "ai-planning" not in tr.ticket["labels"]
        assert tr.comments[-1].startswith("Fix plan failed (opus)")
    finally:
        srv.__exit__(None, None, None)


def test_only_mapped_projects_and_bad_input_are_refused(tmp_path, monkeypatch):
    called: list = []
    tr, cl, w, srv, _ = start(tmp_path, monkeypatch, ["ai-fix"], lambda *a: called.append(a))
    try:
        for inp, why in (({"key": "OTH-1"}, "isn't mapped"), ({"key": ""}, "which issue"),
                         ({"key": "TRK-5", "plan_model": "gpt-4"}, "use opus")):
            job = cl.post("/jobs", {"plugin": "fixer", "workflow": "plan", "needs": ["desktop"], "input": inp})
            assert w.run_once(wait=2)
            dead = wait_for(lambda jid=job["id"]: (j := cl.get(f"/jobs/{jid}"))["state"] in ("succeeded", "dead") and j)
            assert dead["state"] == "dead" and why in dead["error"], (inp, dead["error"])
        assert called == []  # Claude never ran
    finally:
        srv.__exit__(None, None, None)


def test_the_scan_plans_a_labelled_ticket_and_reports_status(tmp_path, monkeypatch):
    def claude(args, prompt, cwd, timeout):
        return PLAN

    tr, cl, w, srv, _ = start(tmp_path, monkeypatch, ["ai-fix"], claude)
    try:
        scan = cl.post("/jobs", {"plugin": "fixer", "workflow": "scan", "needs": ["desktop"], "input": {}})
        for _ in range(8):  # scan marks it and starts the plan as its own job, then waits for it
            w.run_once(wait=0.3)
            if cl.get("/approvals?state=pending"):
                break
        assert [a["title"] for a in cl.get("/approvals?state=pending")] == ["Run the fix for TRK-5 with sonnet?"]
        assert tr.ticket["labels"] == ["ai-fix", "plan-ready"]  # planning came and went
        st = cl.post("/jobs", {"plugin": "fixer", "workflow": "status", "needs": ["desktop"], "input": {}})
        w.run_once(wait=1)
        got = wait_for(lambda: (j := cl.get(f"/jobs/{st['id']}"))["state"] in ("succeeded", "dead") and j)
        assert got["result"]["fixes"] == [{"key": "TRK-5", "title": "Board drops cards",
                                           "stage": "plan ready, waiting for your approval"}]
        assert got["result"]["today"] == 1 and got["result"]["projects"] == ["TRK"]
        assert scan["id"]
    finally:
        srv.__exit__(None, None, None)
