"""The ticket fixer. PLAN: Tracker (fake) -> Claude in the project folder (fake, read-only) -> plan attached to the
ticket -> your approval -> label fix-approved. EXECUTE: a real git repo in a temp folder, a fake Claude that edits the
worktree and saves proof, the real test command -> branch, one commit, proof attached, ticket to review.
Plus the pure rules (models, labels, plan checks, proof checks, commit messages)."""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
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

def finished(cl, job_id: str, w=None, states=("succeeded", "dead", "failed")) -> dict:
    """Run the worker until the job reaches one of `states` (it keeps claiming, so a claim that came to nothing,
    or a job that came back to the queue, is simply tried again). On a timeout say what state it is stuck in."""
    end = time.monotonic() + 40
    while time.monotonic() < end:
        j = cl.get(f"/jobs/{job_id}")
        if j["state"] in states:
            return j
        if w is not None:
            w.run_once(wait=0.5)
        else:
            time.sleep(0.05)
    j = cl.get(f"/jobs/{job_id}")
    raise AssertionError(f"job {job_id} is {j['state']!r}, error: {j.get('error')}")


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


def test_claude_events_become_live_lines_and_the_answer_is_found_in_a_stream():
    events = [
        {"type": "system", "subtype": "init", "model": "claude-opus-4"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking at the sidebar."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "npm run build"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "web/src/App.tsx"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "built in 2s", "is_error": False},
            {"type": "tool_result", "content": [{"type": "text", "text": "boom"}], "is_error": True}]}},
        {"type": "result", "is_error": False, "result": "The plan."},
    ]
    lines = [x for ev in events for x in fx.describe_event(ev)]
    assert lines == [("info", "Claude started (claude-opus-4)"), ("say", "Looking at the sidebar."),
                     ("tool", "Bash npm run build"), ("tool", "Read web/src/App.tsx"), ("result", "built in 2s"),
                     ("error", "boom"), ("done", "The plan.")]
    failed = {"type": "result", "is_error": True, "result": "rate limited"}
    assert fx.describe_event(failed) == [("error", "rate limited")]
    stream = "\n".join(json.dumps(e) for e in events)
    assert fx.claude_text(stream) == "The plan."
    with pytest.raises(fx.PlanError):
        fx.claude_text(stream.replace('"is_error": false, "result": "The plan."', '"is_error": true, "result": "no"'))


def test_a_running_claude_is_streamed_line_by_line_and_can_be_stopped():
    script = (
        "import sys, json, time\n"
        "prompt = sys.stdin.read()\n"
        "say = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'got ' + prompt}]}}\n"
        "print(json.dumps(say), flush=True)\n"
        "time.sleep(float(sys.argv[1]))\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'result': 'ok'}), flush=True)\n")
    got: list[tuple[str, str]] = []
    fx._LIVE.send = lambda kind, text: got.append((kind, text))
    try:
        out, rc, _ = fx.stream_process([sys.executable, "-c", script, "0"], "hello", str(ROOT), 30, "plan")
        assert rc == 0 and fx.claude_text(out) == "ok"
        assert got == [("say", "got hello"), ("done", "ok")]
        with pytest.raises(fx.PlanError, match="the fix took longer than 0 minutes"):
            fx.stream_process([sys.executable, "-c", script, "30"], "x", str(ROOT), 1, "fix")
    finally:
        fx._LIVE.send = None
    with pytest.raises(fx.PlanError, match="wasn't found"):
        fx.stream_process(["no-such-claude-command"], "x", str(ROOT), 5, "plan")


class Tracker:
    def __init__(self, labels):
        self.ticket = {"id": 5, "key": "TRK-5", "title": "Board drops cards", "status": "todo", "priority": 1,
                       "type": "bug", "labels": labels, "description": "Drag a card, it vanishes.",
                       "checklist": [], "due_date": None}
        self.other = {"id": 9, "key": "OTH-1", "title": "Other", "status": "todo", "labels": ["ai-fix"]}
        self.comments: list[str] = []
        self.files: dict[str, str] = {}
        self.blobs: dict[str, bytes] = {}
        self.ids: dict[str, int] = {}
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
                        me.blobs[name] = raw.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
                        me.files[name] = me.blobs[name].decode("utf-8", "replace")
                        me.ids.setdefault(name, 100 + len(me.ids))
                        return self.send(200, {"id": me.ids[name], "filename": name})
                    return self.send(200, [{"id": 1, "filename": "screen.png"}]
                                     + [{"id": i, "filename": n} for n, i in me.ids.items()])
                m = re.fullmatch(r"/api/files/(\d+)/(.+)", p)
                if m and urllib.parse.unquote(m.group(2)) in me.blobs:
                    data = me.blobs[urllib.parse.unquote(m.group(2))]
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(data)
                    return None
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


def start(tmp_path, monkeypatch, labels, claude, test="npm run build", session=None, extra=""):
    tr = Tracker(labels)
    project = tmp_path / "trk"
    project.mkdir()
    (tmp_path / ".env").write_text("TRACKER_API_KEY=k\n", encoding="utf-8")
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: [fixer]\n  config:\n    fixer:\n"
        f"      tracker_url: '{tr.url}'\n      claude_command: fakeclaude\n      plan_model: opus\n{extra}"
        f"      projects:\n        - 'TRK | {project.as_posix()} | {test} | opus | sonnet'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    srv = Server(Argus(load_config(tmp_path / "argus.yaml")).open())
    srv.__enter__()
    cl = client(srv.url)
    # The plugin's own two-minute scan schedule must not fire during a test: the worker would pick its job before
    # the test's (that made these tests fail whenever a run crossed an even minute).
    with sqlite3.connect(tmp_path / "data" / "argus.db", timeout=10) as db:
        db.execute("UPDATE schedules SET enabled = 0")
    w = Worker(cl, "pc", capabilities=["desktop"], watch_folders=False)
    w.register()
    assert "fixer" in w.plugins, w.plugin_errors
    monkeypatch.setattr(sys.modules["argus_plugin_fixer"], "run_claude", claude)
    if session:
        monkeypatch.setattr(sys.modules["argus_plugin_fixer"], "run_session", session)
    return tr, cl, w, srv, project


def post(cl, workflow, **inp):
    return cl.post("/jobs", {"plugin": "fixer", "workflow": workflow, "needs": ["desktop"], "input": inp})


def test_plan_attached_then_approved(tmp_path, monkeypatch):
    seen: list[dict] = []
    answers = ["## Summary\nshort", PLAN]  # the first answer is refused, the second is used

    def claude(args, prompt, cwd, timeout):
        seen.append({"args": args, "prompt": prompt, "cwd": cwd, "timeout": timeout})
        return answers[len(seen) - 1]

    tr, cl, w, srv, project = start(tmp_path, monkeypatch, ["ai-fix", "plan:haiku"], claude)
    try:
        job = post(cl, "plan", key="trk-5")
        done = finished(cl, job["id"], w)
        assert done["state"] == "succeeded", done["error"]
        assert len(seen) == 2 and "refused" in seen[1]["prompt"] and "too short" in seen[1]["prompt"]
        assert seen[0]["cwd"] == project.as_posix() and seen[0]["timeout"] == 600
        assert seen[0]["args"][seen[0]["args"].index("--model") + 1] == "haiku"  # the ticket's label
        assert "Board drops cards" in seen[0]["prompt"] and "seen on staging" in seen[0]["prompt"]
        plan_file = tr.files["TRK-5-fix-plan.md"]
        assert plan_file.count("## Summary") == 1 and "fix model: sonnet" in plan_file
        assert "plan-ready" in tr.ticket["labels"] and "ai-planning" not in tr.ticket["labels"]
        assert tr.comments[0].startswith("Fix plan ready (plan: haiku, fix: sonnet)")
        assert "run the fix for TRK-5" in tr.comments[0] and cl.get("/approvals?state=pending") == []
        # the question is its own job, so waiting for your answer never blocks the scan
        ask = post(cl, "ask")
        assert finished(cl, ask["id"], w, states=("waiting", "succeeded", "dead", "failed"))["state"] == "waiting"
        a = cl.get("/approvals?state=pending")[0]
        assert a["title"] == "Run the fix for TRK-5 with sonnet?"
        cl.post(f"/approvals/{a['id']}/decide", {"answer": "approve"})
        wait_for(lambda: cl.get(f"/jobs/{ask['id']}")["state"] == "queued")
        done = finished(cl, ask["id"], w)
        assert done["state"] == "succeeded", done["error"]
        assert done["result"]["approved"] is True and done["result"]["asked"] == "TRK-5"
        assert "fix-approved" in tr.ticket["labels"] and "plan-ready" not in tr.ticket["labels"]
        assert "ai-fix" in tr.ticket["labels"] and len(seen) == 2  # not planned again after the approval
        again = post(cl, "ask")  # asked about this plan already
        assert finished(cl, again["id"], w)["result"] == {"asked": None}
    finally:
        srv.__exit__(None, None, None)


def test_rejected_and_failed_plans_say_so_on_the_ticket(tmp_path, monkeypatch):
    def claude(args, prompt, cwd, timeout):
        return PLAN

    tr, cl, w, srv, _ = start(tmp_path, monkeypatch, ["ai-fix"], claude)
    try:
        job = post(cl, "plan", key="TRK-5")
        assert finished(cl, job["id"], w)["state"] == "succeeded"
        ask = post(cl, "ask")
        assert finished(cl, ask["id"], w, states=("waiting", "succeeded", "dead", "failed"))["state"] == "waiting"
        cl.post(f"/approvals/{cl.get('/approvals?state=pending')[0]['id']}/decide", {"answer": "reject"})
        wait_for(lambda: cl.get(f"/jobs/{ask['id']}")["state"] == "queued")
        done = finished(cl, ask["id"], w)
        assert done["result"]["approved"] is False and "plan-rejected" in tr.ticket["labels"]
        assert tr.comments[-1].startswith("Fix not approved")

        def broken(args, prompt, cwd, timeout):
            raise sys.modules["argus_plugin_fixer"].PlanError("the plan took longer than 10 minutes and was stopped")

        monkeypatch.setattr(sys.modules["argus_plugin_fixer"], "run_claude", broken)
        job = post(cl, "plan", key="TRK-5")
        dead = finished(cl, job["id"], w)
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
            dead = finished(cl, job["id"], w)
            assert dead["state"] == "dead" and why in dead["error"], (inp, dead["error"])
        assert called == []  # Claude never ran
    finally:
        srv.__exit__(None, None, None)


def test_the_scan_plans_a_labelled_ticket_and_reports_status(tmp_path, monkeypatch):
    def claude(args, prompt, cwd, timeout):
        return PLAN

    tr, cl, w, srv, _ = start(tmp_path, monkeypatch, ["ai-fix"], claude)
    try:
        scan = post(cl, "scan")
        done = finished(cl, scan["id"], w)  # the scan plans it itself and ends: it never waits for your answer
        assert done["state"] == "succeeded" and done["result"]["planned"] == "TRK-5", done
        assert tr.ticket["labels"] == ["ai-fix", "plan-ready"]  # planning came and went
        assert cl.get("/approvals?state=pending") == []
        ask = post(cl, "ask")
        assert finished(cl, ask["id"], w, states=("waiting", "succeeded", "dead", "failed"))["state"] == "waiting"
        assert [a["title"] for a in cl.get("/approvals?state=pending")] == ["Run the fix for TRK-5 with sonnet?"]
        for inp, rows in (({}, 1), ({"key": "trk-5"}, 1), ({"key": "TRK-9"}, 0)):
            st = post(cl, "status", **inp)
            got = finished(cl, st["id"], w)
            assert len(got["result"]["fixes"]) == rows, inp
        assert got["result"]["today"] == 1 and got["result"]["projects"] == ["TRK"]
        assert got["result"]["tracker"].startswith("http://127.0.0.1:")  # the Helios tab links to it
        assert got["result"]["fixes"] == [] and fx.stage(["ai-fix", "plan-ready"]).startswith("plan ready")
    finally:
        srv.__exit__(None, None, None)


def test_ari_queues_and_approves_without_waiting(tmp_path, monkeypatch):
    called: list = []
    tr, cl, w, srv, _ = start(tmp_path, monkeypatch, ["bug"], lambda *a: called.append(a))
    try:
        def run(workflow, **inp):
            job = post(cl, workflow, **inp)
            return finished(cl, job["id"], w)

        done = run("queue", key="trk-5", plan_model="Haiku", fix_model="opus")
        assert done["state"] == "succeeded", done["error"]
        assert done["result"] == {"queued": "TRK-5", "plan_model": "haiku", "fix_model": "opus"}
        assert tr.ticket["labels"] == ["bug", "plan:haiku", "fix:opus", "ai-fix"]
        assert tr.comments[-1].startswith("Queued for an AI fix from Ari (plan: haiku, fix: opus)")
        for inp, why in (({"key": "OTH-1"}, "isn't mapped"), ({"key": "TRK-5", "plan_model": "gpt-4"}, "use opus")):
            assert run("queue", **inp)["error"].find(why) >= 0
        tr.ticket["labels"] = ["ai-fix", "ai-planning"]
        assert "already being worked on (planning)" in run("queue", key="TRK-5")["error"]
        # run_fix: needs a plan first, then only marks the ticket (the scan starts the work)
        tr.ticket["labels"] = ["ai-fix", "plan-ready"]
        assert "no fix plan attached" in run("approve", key="TRK-5")["error"]
        tr.blobs["TRK-5-fix-plan.md"] = PLAN.encode()
        tr.ids["TRK-5-fix-plan.md"] = 7
        ok = run("approve", key="TRK-5", fix_model="opus")
        assert ok["state"] == "succeeded" and ok["result"] == {"approved": "TRK-5", "fix_model": "opus"}
        assert tr.ticket["labels"] == ["ai-fix", "fix-approved"]
        assert tr.comments[-1].startswith("Fix approved from Ari (opus)") and called == []
    finally:
        srv.__exit__(None, None, None)


def test_a_limit_keeps_the_ticket_queued_and_a_broken_set_up_is_shown(tmp_path, monkeypatch):
    def claude(args, prompt, cwd, timeout):
        return PLAN

    tr, cl, w, srv, project = start(tmp_path, monkeypatch, ["ai-fix"], claude, extra="      fixes_per_day: 1\n")
    try:
        def scan():
            job = post(cl, "scan")
            return finished(cl, job["id"], w)

        assert scan()["result"]["planned"] == "TRK-5"
        tr.ticket["labels"] = ["ai-fix"]  # asked to plan again, but today's one run is used up
        got = scan()
        assert got["state"] == "succeeded" and "already 1 plans and fixes today" in got["result"]["deferred"]
        assert tr.ticket["labels"] == ["ai-fix"]  # still queued: not stuck on ai-planning, not failed
        # an approved ticket whose plan is missing: the scan says so on the ticket instead of leaving ai-fixing
        make_repo(project)
        tr.ticket["labels"] = ["ai-fix", "fix-approved"]
        tr.blobs.pop("TRK-5-fix-plan.md")
        tr.ids.pop("TRK-5-fix-plan.md")
        got = scan()
        assert "no fix plan attached" in got["result"]["failed"]
        assert "fix-failed" in tr.ticket["labels"] and "ai-fixing" not in tr.ticket["labels"]
        assert tr.comments[-1].startswith("The fix didn't run: ")
    finally:
        srv.__exit__(None, None, None)


# ---------------------------------------------------------------------- the execute phase

cspec = importlib.util.spec_from_file_location("change_rules", ROOT / "scripts" / "change.py")
change = importlib.util.module_from_spec(cspec)
cspec.loader.exec_module(change)

CHECK = 'import os, sys\nopen("build.out", "w").write("artifact")\nsys.exit(0 if os.path.exists("fixed.txt") else 1)\n'
GOOD_PROOF = {
    "TRK-5-before-board.png": b"PNG-before", "TRK-5-after-board.png": b"PNG-after",
    "TRK-5-board.log": "GET /board 200", "notes.docx": "not an allowed name",
    "proof.md": "- [x] step 1: the card stays on drop (TRK-5-board.log)\n\n## How to check it yourself\nDrag a card.\n"}


def test_branch_commit_and_proof_rules():
    assert fx.branch_name("TRK-5", "Board drops cards!") == "fix/trk-5-board-drops-cards"
    long = fx.branch_name("TRK-5", "A very long title about the board that keeps going and going and going")
    assert change.branch_problem(long) is None and len(long) <= 48 + 4
    ticket = {"key": "TRK-5", "type": "bug", "title": "Board drops cards when you drag them"}
    assert fx.commit_title("TRK", ticket) == "fix(trk): TRK-5 board drops cards when you drag them"
    for tk in (ticket, {**ticket, "type": "feature", "title": "x " * 60 + "."}, {**ticket, "type": "task"}):
        title = fx.commit_title("TRK", tk)
        assert change.title_problem(title) is None and len(title) <= 72, title
    assert fx.commit_title("TRK", {**ticket, "type": "feature"}).startswith("feat(trk): ")
    body = fx.commit_body(PLAN, "npm test", 1, 0, ["a.py"])
    assert change.body_problem(body) is None and "failed (exit 1)" in body and "passed" in body
    assert fx.forbidden(["src/app.py", ".env", "web/.env.local", "keys/server.pem", ".github/workflows/ci.yml",
                         "docs/credentials.md", "a/.git/config"]) == [".env", "web/.env.local", "keys/server.pem",
                                                                       ".github/workflows/ci.yml",
                                                                       "docs/credentials.md", "a/.git/config"]
    assert fx.forbidden(["src/app.py", "README.md", "web/src/Board.tsx"]) == []

    ok = {"proof.md": 10, "TRK-5-before-board.png": 5, "TRK-5-after-board.png": 5}
    md = "- [x] one\n- [x] two\n"
    plan = PLAN.replace("## Steps\n", "## Steps\n1. first\n2. second\n")
    assert fx.plan_steps(plan) == 2
    assert fx.proof_problems("TRK-5", ok, plan, ["ui"], md, test="npm test", tests_ok=True) == []
    got = fx.proof_problems("TRK-5", {"proof.md": 10}, plan, ["ui"], "- [x] one\n", test="npm test", tests_ok=False)
    assert len(got) == 3 and "tests fail" in got[0] and "1 line(s) for 2" in got[1] and "before-<what>" in got[2]
    assert "proof.md is missing" in fx.proof_problems("TRK-5", {}, plan, [], "", test="", tests_ok=True)[0]
    assert fx.needs_screenshots(["bug"], "Take a screenshot of the board") and not fx.needs_screenshots(["bug"], "none")
    assert fx.proof_kind("TRK-5-before-board.png", "TRK-5") == "shot" and fx.proof_kind("proof.md", "TRK-5") == "notes"
    for bad in ("TRK-5-before-board.exe", "OTHER-1-board.log", "TRK-5-Board.log", "x.png", "TRK-5-before-.png"):
        assert fx.proof_kind(bad, "TRK-5") == "", bad


def test_the_fix_command_and_the_session_log():
    args = fx.fix_args("claude", "sonnet", ["npm run *", "git diff*"], "/p/proof")
    allowed = args[args.index("--allowedTools") + 1].split(",")
    deny = args[args.index("--disallowedTools") + 1].split(",")
    assert allowed == ["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "Bash(npm run *)", "Bash(git diff*)"]
    assert {"WebFetch", "WebSearch", "Bash(git commit*)", "Bash(git push*)", "Read(**/.env*)",
            "Edit(**/.env*)"} <= set(deny)
    assert args[args.index("--add-dir") + 1] == "/p/proof" and "acceptEdits" in args and "stream-json" in args
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "curl -H 'api_key: abcd1234' x"}},
        {"type": "text", "text": "token=SECRET123 and sk-abcdefghijklmnopqrstuv"}]}},
        {"type": "result", "is_error": True, "result": "max turns reached"}]
    got = fx.parse_stream("not json\n" + "\n".join(json.dumps(e) for e in events), ("SECRET123",))
    assert "abcd1234" not in got["log"] and "SECRET123" not in got["log"] and "sk-abcdef" not in got["log"]
    assert "Bash:" in got["log"] and got["error"] == "max turns reached"
    tickets = [{"key": "TRK-1", "labels": ["ai-fix", "fix-approved"], "status": "todo"},
               {"key": "TRK-2", "labels": ["fix-approved", "ai-fixing"], "status": "todo"},
               {"key": "TRK-3", "labels": ["fix-approved", "fix-done"], "status": "todo"},
               {"key": "OTH-4", "labels": ["fix-approved"], "status": "todo"}]
    assert [t["key"] for t in fx.ready_to_fix(tickets, {"TRK": {}})] == ["TRK-1"]
    assert fx.stage(["ai-fix", "fix-approved", "fix-done"]) == "fix done, in review"


def make_repo(project: Path) -> str:
    def g(*a):
        return subprocess.run(["git", *a], cwd=project, check=True, capture_output=True, text=True).stdout.strip()

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    (project / "app.txt").write_text("v1\n")
    (project / "check.py").write_text(CHECK)
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    return g("rev-parse", "main")


def fake_session(files, proof=None, final="Kept the card on drop.", boom=None):
    calls: list[dict] = []

    def run(args, prompt, cwd, timeout):
        calls.append({"args": args, "prompt": prompt, "cwd": cwd, "timeout": timeout})
        if boom:
            raise sys.modules["argus_plugin_fixer"].PlanError(boom)
        for name, text in files.items():
            (Path(cwd) / name).write_text(text)
        for name, data in (proof or {}).items():
            target = Path(args[args.index("--add-dir") + 1]) / name
            target.write_bytes(data if isinstance(data, bytes) else data.encode())
        events = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "app.txt"}},
            {"type": "text", "text": "password: hunter2 then done"}]}},
            {"type": "result", "is_error": False, "result": final}]
        return "\n".join(json.dumps(e) for e in events)

    run.calls = calls
    return run


def run_fix(cl, w, key="TRK-5", **extra):
    job = cl.post("/jobs", {"plugin": "fixer", "workflow": "fix", "needs": ["desktop"], "input": {"key": key, **extra}})
    return finished(cl, job['id'], w)


def git_out(project, *a):
    return subprocess.run(["git", *a], cwd=project, check=True, capture_output=True, text=True).stdout.strip()


def test_the_fix_lands_on_a_branch_with_proof_attached(tmp_path, monkeypatch):
    session = fake_session({"fixed.txt": "yes\n", "app.txt": "v2\n"}, GOOD_PROOF)
    tr, cl, w, srv, project = start(tmp_path, monkeypatch, ["ai-fix", "fix-approved"], None, test="python check.py",
                                    session=session)
    try:
        main_before = make_repo(project)
        tr.blobs["TRK-5-fix-plan.md"] = PLAN.encode()
        tr.ids["TRK-5-fix-plan.md"] = 7
        done = run_fix(cl, w)
        assert done["state"] == "succeeded", done["error"]
        assert done["result"]["ok"] is True and done["result"]["branch"] == "fix/trk-5-board-drops-cards"
        # Claude's call: the worktree, the models and the rules
        call = session.calls[0]
        assert "work" in Path(call["cwd"]).parts and call["cwd"] != str(project) and call["timeout"] == 1800
        assert call["args"][call["args"].index("--model") + 1] == "sonnet"
        assert "Bash(npm run *)" in call["args"][call["args"].index("--allowedTools") + 1]
        assert "Bash(git commit*)" in call["args"][call["args"].index("--disallowedTools") + 1]
        assert "TRK-5-before-<what>.png" in call["prompt"] and "Details about it." in call["prompt"]
        # the branch: one commit with a change-flow message, no build output, main untouched
        branch = done["result"]["branch"]
        assert git_out(project, "rev-parse", "main") == main_before
        assert git_out(project, "log", "-1", "--format=%s", branch) == "fix(trk): TRK-5 board drops cards"
        message = git_out(project, "log", "-1", "--format=%B", branch)
        assert change.body_problem(message) is None and "claude" not in message.lower()
        assert sorted(git_out(project, "diff", "--name-only", f"main...{branch}").split()) == ["app.txt", "fixed.txt"]
        assert git_out(project, "rev-list", "--count", f"main..{branch}") == "1"
        assert len(git_out(project, "worktree", "list").splitlines()) == 1  # the worktree is gone, the branch stays
        # the ticket: proof attached, nothing else, status and labels
        names = set(tr.files) - {"TRK-5-fix-plan.md"}
        assert names == {"TRK-5-before-board.png", "TRK-5-after-board.png", "TRK-5-board.log", "proof.md",
                         "TRK-5-tests-before.log", "TRK-5-tests.log", "TRK-5-fix.diff", "TRK-5-session.log",
                         "TRK-5-fix-report.md"}
        assert tr.blobs["TRK-5-after-board.png"] == b"PNG-after"
        assert "exit code 1" in tr.files["TRK-5-tests-before.log"] and "exit code 0" in tr.files["TRK-5-tests.log"]
        assert "hunter2" not in tr.files["TRK-5-session.log"] and "Edit:" in tr.files["TRK-5-session.log"]
        assert "+yes" in tr.files["TRK-5-fix.diff"] and "build.out" not in tr.files["TRK-5-fix.diff"]
        assert "ready for review" in tr.files["TRK-5-fix-report.md"]
        assert tr.ticket["status"] == "review" and "fix-done" in tr.ticket["labels"]
        assert not {"ai-fixing", "fix-approved"} & set(tr.ticket["labels"])
        assert tr.comments[0].startswith("Fix started with sonnet on branch `fix/trk-5-board-drops-cards`")
        assert tr.comments[-1].startswith("Fix ready for review (sonnet)") and "nothing was pushed" in tr.comments[-1]
        # the live view: the run's lines are events the Helios Fixes tab can show, in order
        evs = cl.get("/events?kinds=plugin.fixer.live&limit=100")["events"]
        lines = [(e["data"]["kind"], e["data"]["text"]) for e in evs if e["data"]["key"] == "TRK-5"]
        assert lines[0] == ("info", "Fixing TRK-5 with sonnet")
        assert any(t.startswith("Working on branch fix/trk-5-board-drops-cards") for _, t in lines)
        assert any(k == "error" and "tests failed" in t for k, t in lines)  # the tests before the fix fail
        assert any(k == "result" and "tests passed" in t for k, t in lines)
        assert lines[-1][0] == "done" and {e["data"]["phase"] for e in evs} == {"fix"}
    finally:
        srv.__exit__(None, None, None)


def test_missing_proof_or_forbidden_files_keep_the_ticket_in_progress(tmp_path, monkeypatch):
    session = fake_session({"fixed.txt": "yes\n"}, {"TRK-5-board.log": "ok"})
    tr, cl, w, srv, project = start(tmp_path, monkeypatch, ["ai-fix", "fix-approved", "ui"], None,
                                    test="python check.py", session=session)
    try:
        make_repo(project)
        tr.blobs["TRK-5-fix-plan.md"] = PLAN.encode()
        tr.ids["TRK-5-fix-plan.md"] = 7
        done = run_fix(cl, w)
        assert done["state"] == "succeeded" and done["result"]["ok"] is False
        assert any("proof.md is missing" in p for p in done["result"]["problems"])
        assert any("before-<what>" in p for p in done["result"]["problems"])
        assert "fix-failed" in tr.ticket["labels"] and "fix-done" not in tr.ticket["labels"]
        assert tr.ticket["status"] == "in_progress"
        assert tr.comments[-1].startswith("Fix not finished (sonnet). Missing:") and "proof.md" in tr.comments[-1]
        assert git_out(project, "log", "-1", "--format=%s", "fix/trk-5-board-drops-cards") == \
            "chore(trk): wip TRK-5 not finished"
        assert "TRK-5-tests.log" in tr.files and "TRK-5-board.log" in tr.files  # what exists is attached
        # try again: the branch name is never reused
        tr.ticket["labels"] = ["ai-fix", "fix-approved"]
        session.calls.clear()
        again = fake_session({"fixed.txt": "yes\n", ".env": "KEY=1\n", "app.txt": "v2\n"}, GOOD_PROOF)
        monkeypatch.setattr(sys.modules["argus_plugin_fixer"], "run_session", again)
        second = run_fix(cl, w)
        assert second["result"]["branch"] == "fix/trk-5-board-drops-cards-2" and second["result"]["ok"] is False
        assert "may never touch" in second["result"]["problems"][0] and ".env" in second["result"]["problems"][0]
        assert ".env" not in git_out(project, "diff", "--name-only", "main...fix/trk-5-board-drops-cards-2").split()
    finally:
        srv.__exit__(None, None, None)


def test_a_claude_error_and_bad_set_ups_are_reported(tmp_path, monkeypatch):
    session = fake_session({}, None, boom="the fix took longer than 30 minutes and was stopped")
    tr, cl, w, srv, project = start(tmp_path, monkeypatch, ["ai-fix", "fix-approved"], None, session=session)
    try:
        dead = run_fix(cl, w)  # the folder isn't a git repo
        assert dead["state"] == "dead" and "git repository" in dead["error"] and session.calls == []
        make_repo(project)
        dead = run_fix(cl, w)  # git is fine, but no plan was attached
        assert dead["state"] == "dead" and "no fix plan attached" in dead["error"]
        tr.blobs["TRK-5-fix-plan.md"] = b"## Summary\nshort"
        tr.ids["TRK-5-fix-plan.md"] = 7
        dead = run_fix(cl, w)
        assert dead["state"] == "dead" and "can't be used" in dead["error"] and session.calls == []
        tr.blobs["TRK-5-fix-plan.md"] = PLAN.encode()
        done = run_fix(cl, w)
        assert done["result"]["ok"] is False
        assert done["result"]["problems"][0].startswith("Claude stopped early: the fix took longer than 30 minutes")
        assert "no files were changed" in done["result"]["problems"]
        assert "fix-failed" in tr.ticket["labels"] and len(session.calls) == 1
        assert "isn't mapped" in run_fix(cl, w, key="OTH-1")["error"]
    finally:
        srv.__exit__(None, None, None)


def test_the_scan_runs_an_approved_fix(tmp_path, monkeypatch):
    session = fake_session({"fixed.txt": "yes\n", "app.txt": "v2\n"}, GOOD_PROOF)
    planner: list = []
    tr, cl, w, srv, project = start(tmp_path, monkeypatch, ["ai-fix", "fix-approved"], lambda *a: planner.append(a),
                                    test="python check.py", session=session)
    try:
        make_repo(project)
        tr.blobs["TRK-5-fix-plan.md"] = PLAN.encode()
        tr.ids["TRK-5-fix-plan.md"] = 7
        scan = post(cl, "scan")
        done = finished(cl, scan["id"], w)  # the scan does the whole fix itself
        assert done["state"] == "succeeded" and done["result"]["fixed"] == "TRK-5" and done["result"]["ok"] is True
        assert "fix-done" in tr.ticket["labels"] and tr.ticket["status"] == "review"
        assert len(session.calls) == 1 and planner == []  # fixed, and nothing was planned
        st = post(cl, "status")
        got = finished(cl, st["id"], w)
        assert got["result"]["fixes"][0]["stage"] == "fix done, in review" and got["result"]["today"] == 1
    finally:
        srv.__exit__(None, None, None)
