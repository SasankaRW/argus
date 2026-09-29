"""C7: model providers, the tier router, circuit breakers, the Claude budget, and escalation end to end."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from argus.api import create_app
from argus.config import Config
from argus.events import read_events
from argus.modelboard import ModelBoard
from argus.models import (
    ClaudeProvider,
    EscalationExhausted,
    ModelTimeout,
    ModelUnavailable,
    OllamaProvider,
    Router,
    build_providers,
    parse_json,
)
from argus.worker import Worker, WorkflowRegistry, workflow
from conftest import run
from fakes import FakeOllama, fake_claude
from test_worker import Server, client, make

GOOD = {"category": "Documents", "reason": "a lecture"}
WRONG = {"category": "Lectures", "reason": "course file"}
CATS = {"Documents", "Images"}


class Placement(BaseModel):
    category: str
    reason: str


def check(p: Placement, _inp) -> str | None:
    return None if p.category in CATS else f"{p.category!r} is not allowed"


class FakeBoard:
    def __init__(self, deny: dict[str, str] | None = None):
        self.deny = deny or {}
        self.reports: list[tuple] = []
        self.events: list[tuple] = []

    def permit(self, tier):
        return {"allowed": tier not in self.deny, "reason": self.deny.get(tier)}

    def report(self, tier, ok, latency_ms, error):
        self.reports.append((tier, ok, error))

    def event(self, kind, src, dst, data):
        self.events.append((kind, src, dst))


# ------------------------------------------------------------------ providers


def test_parse_json_forgives_fences_and_chatter():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! Here it is: {"a": 2} Hope that helps.') == {"a": 2}
    with pytest.raises(ValueError):
        parse_json("no json here")


def test_ollama_provider_ok_timeout_missing_error():
    with FakeOllama({"good": [GOOD], "hang": ["HANG"], "err": ["ERROR"]}) as ol:
        r = OllamaProvider(ol.url, "good", keep_alive="7m").chat("sys", [{"role": "user", "content": "hi"}],
                                                                 Placement.model_json_schema())
        assert json.loads(r.text) == GOOD and r.latency_ms >= 0
        sent = ol.requests[-1]
        assert sent["stream"] is False and sent["keep_alive"] == "7m" and sent["options"]["temperature"] == 0
        assert sent["format"]["properties"]["category"] and sent["messages"][0]["role"] == "system"
        t0 = time.perf_counter()
        with pytest.raises(ModelTimeout):
            OllamaProvider(ol.url, "hang", timeout=0.3).chat("s", [{"role": "user", "content": "x"}])
        assert time.perf_counter() - t0 < 2  # a hung model never hangs the worker
        with pytest.raises(ModelUnavailable, match="ollama pull nope"):
            OllamaProvider(ol.url, "nope").chat("s", [{"role": "user", "content": "x"}])
        with pytest.raises(ModelUnavailable, match="500"):
            OllamaProvider(ol.url, "err").chat("s", [{"role": "user", "content": "x"}])
        assert set(OllamaProvider(ol.url, "").list_models()) == {"good", "hang", "err"}
    with pytest.raises(ModelUnavailable, match="not reachable"):
        OllamaProvider("http://127.0.0.1:9", "x", timeout=10).chat("s", [{"role": "user", "content": "x"}])


def test_claude_provider_runs_with_tools_off(tmp_path):
    cfg = Config().claude
    p = ClaudeProvider(fake_claude(tmp_path), cfg.args, timeout=5)
    r = p.chat("be brief", [{"role": "user", "content": "classify lecture_07.pdf"}], Placement.model_json_schema())
    assert json.loads(r.text) == {"category": "Documents", "reason": "claude"}
    argv = json.loads((tmp_path / "claude_argv.json").read_text())
    assert "-p" in argv and argv[argv.index("--disallowedTools") + 1] == "*"
    assert argv[argv.index("--output-format") + 1] == "json" and "--append-system-prompt" not in argv
    stdin = (tmp_path / "claude_stdin.txt").read_text(encoding="utf-8")
    assert stdin.startswith("be brief") and "classify lecture_07.pdf" in stdin  # the playbook goes via stdin
    with pytest.raises(ModelTimeout):
        hung = ClaudeProvider(fake_claude(tmp_path, "hang"), cfg.args, timeout=0.5)
        hung.chat("s", [{"role": "user", "content": "x"}])
    with pytest.raises(ModelUnavailable, match="reported an error"):
        ClaudeProvider(fake_claude(tmp_path, "error"), cfg.args).chat("s", [{"role": "user", "content": "x"}])
    with pytest.raises(ModelUnavailable, match="login"):
        ClaudeProvider(fake_claude(tmp_path, "crash"), cfg.args).chat("s", [{"role": "user", "content": "x"}])
    assert not ClaudeProvider(["definitely-not-claude-xyz"], []).available()


# ------------------------------------------------------------------ router


def providers(ol, t1="t1", t2="t2"):
    return {"T1": OllamaProvider(ol.url, t1, timeout=0.5), "T2": OllamaProvider(ol.url, t2, timeout=0.5)}


def test_router_answers_at_the_cheapest_tier():
    with FakeOllama({"t1": [GOOD], "t2": [GOOD]}) as ol:
        b = FakeBoard()
        ans = Router(providers(ol), b, chain=["T1", "T2"], source="demo").ask("sort", {"f": "x"}, schema=Placement,
                                                                             check=check)
        assert ans.tier == "T1" and ans.value.category == "Documents" and ans.attempts == 1
        assert [e[0] for e in b.events] == ["model.request", "model.reply"]
        assert b.events[0][1:] == ("demo", "t1") and b.events[1][1:] == ("t1", "demo")


def test_router_retries_with_feedback_then_escalates_with_advice():
    with FakeOllama({"t1": ["not json at all", WRONG], "t2": [GOOD]}) as ol:
        b = FakeBoard()
        ans = Router(providers(ol), b, chain=["T1", "T2"], source="demo").ask("sort", {"f": "lecture"},
                                                                             schema=Placement, check=check)
        assert ans.tier == "T2" and ans.value.category == "Documents"
        kinds = [e[0] for e in b.events]
        assert kinds.count("check.failed") == 2 and ("model.escalated", "t1", "t2") in b.events
        # the second T1 try was told what was wrong; T2 was told what T1 answered and why it failed
        t1_retry = [r for r in ol.requests if r["model"] == "t1"][1]["messages"]
        assert "rejected" in t1_retry[-1]["content"] and t1_retry[-2]["role"] == "assistant"
        t2_prompt = [r for r in ol.requests if r["model"] == "t2"][0]["messages"][1]["content"]
        assert "Lectures" in t2_prompt and "not allowed" in t2_prompt
        # wrong answers are not breaker failures: the models were up
        assert all(ok for _, ok, _ in b.reports)


def test_router_moves_on_when_a_tier_hangs_errors_or_is_refused():
    with FakeOllama({"t1": ["HANG"], "t2": [GOOD]}) as ol:
        b = FakeBoard()
        t0 = time.perf_counter()
        ans = Router(providers(ol), b, chain=["T1", "T2"]).ask("sort", "x", schema=Placement, check=check)
        assert ans.tier == "T2" and time.perf_counter() - t0 < 3
        assert b.reports[0][:2] == ("T1", False) and "model.timeout" in [e[0] for e in b.events]
        assert len([r for r in ol.requests if r["model"] == "t1"]) == 1  # no pointless retry of a dead model
    with FakeOllama({"t2": [GOOD]}) as ol:
        b = FakeBoard(deny={"T1": "breaker open"})
        ans = Router(providers(ol), b, chain=["T1", "T2"]).ask("sort", "x", schema=Placement)
        assert ans.tier == "T2" and ans.trail[0] == {"tier": "T1", "attempt": 1, "skipped": "breaker open"}
        assert not any(r["model"] == "t1" for r in ol.requests)


def test_router_gives_up_cleanly_and_survives_a_crashing_check():
    with FakeOllama({"t1": [WRONG], "t2": ["ERROR"]}) as ol:
        with pytest.raises(EscalationExhausted) as e:
            Router(providers(ol), FakeBoard(), chain=["T1", "T2"]).ask("sort", "x", schema=Placement, check=check)
        assert [t["tier"] for t in e.value.trail] == ["T1", "T1", "T2"]

    def boom(_v, _i):
        raise KeyError("oops")

    with FakeOllama({"t1": [GOOD], "t2": [GOOD]}) as ol:
        with pytest.raises(EscalationExhausted) as e:
            Router(providers(ol), FakeBoard(), chain=["T1", "T2"], attempts_per_tier=1).ask("s", "x", schema=Placement,
                                                                                           check=boom)
        assert "check crashed" in e.value.trail[0]["rejected"]


def test_text_answers_without_schema():
    with FakeOllama({"t1": ["  plain words  "]}) as ol:
        ans = Router({"T1": OllamaProvider(ol.url, "t1")}, FakeBoard(), chain=["T1"]).ask("say hi", "x")
        assert ans.value == "  plain words  "


# ------------------------------------------------------------------ board: breakers and budget


def board(store, clock, **models) -> ModelBoard:
    cfg = Config(models={"breaker_failures": 2, "breaker_open_seconds": 60, **models}, claude={"calls_per_day": 2})
    return ModelBoard(store, cfg, clock=clock)


def test_breaker_opens_then_half_opens_then_closes(store, clock):
    mb = board(store, clock)

    async def scenario():
        await mb.register()
        assert (await mb.permit("T1")).allowed
        await mb.report("T1", False, error="timeout")
        assert (await mb.permit("T1")).allowed  # one failure: still closed
        await mb.report("T1", False, error="timeout")
        p = await mb.permit("T1")
        assert not p.allowed and p.reason == "breaker open" and p.retry_after == 60
        clock.advance(61)
        assert (await mb.permit("T1")).allowed  # half open: one trial call
        await mb.report("T1", False, error="still down")  # trial fails: open again at once
        assert not (await mb.permit("T1")).allowed
        clock.advance(61)
        assert (await mb.permit("T1")).allowed
        await mb.report("T1", True, latency_ms=900)
        snap = await mb.snapshot()
        t1 = next(t for t in snap["tiers"] if t["tier"] == "T1")
        assert t1["state"] == "closed" and t1["failures"] == 3 and t1["calls"] == 4
        return snap

    snap = run(scenario())
    kinds = [e["kind"] for e in store.read_sync(lambda c: read_events(c, 0))]
    assert kinds.count("model.breaker_opened") == 2 and "model.breaker_closed" in kinds
    assert {n["tier"] for n in snap["tiers"]} == {"T1", "T2", "T3"}


def test_claude_daily_cap_and_reset(store, clock):
    mb = board(store, clock)

    async def scenario():
        assert (await mb.permit("T3")).allowed and (await mb.permit("T3")).allowed
        p = await mb.permit("T3")
        assert not p.allowed and "cap" in p.reason
        assert (await mb.permit("T1")).allowed  # local tiers have no budget
        assert (await mb.snapshot())["claude"] == {"calls_today": 2, "calls_per_day": 2}
        clock.advance(86400)
        assert (await mb.permit("T3")).allowed  # a new day
        assert not (await mb.permit("T9")).allowed

    run(scenario())


def test_models_on_the_map(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        nodes = {n["id"]: n for n in c.get("/map").json()["nodes"]}
        assert nodes["t1"]["kind"] == "model" and nodes["t1"]["label"] == "T1 · qwen2.5-coder:7b"
        assert nodes["t3"]["group"] == "cloud" and nodes["t1"]["state"] == "closed"
        m = c.get("/models").json()
        assert m["chain"] == ["T1", "T2", "T3"] and m["claude"]["calls_per_day"] == 30
        assert c.post("/models/T1/permit", json={}).json()["allowed"] is True
        assert c.post("/models/T1/report", json={"ok": True, "latency_ms": 12}).json() == {"state": "closed"}
        assert c.post("/models/T7/report", json={"ok": True}).status_code == 404
        reg = c.post("/workers/register", json={"id": "w", "host": "pc"}).json()
        assert reg["models"]["tiers"]["T2"]["model"] == "qwen2.5-coder:14b"
        assert "--disallowedTools" in reg["models"]["claude"]["args"]


def test_trace_events_need_the_lease_and_a_known_kind(tmp_path):
    argus = make(tmp_path).open()
    with TestClient(create_app(argus)) as c:
        jid = c.post("/jobs", json={"plugin": "demo", "workflow": "x"}).json()["id"]
        c.post("/workers/w/claim", json={})
        ev = {"worker": "w", "kind": "model.request", "src": "demo", "dst": "t1", "data": {"tier": "T1"}}
        assert c.post(f"/jobs/{jid}/events", json=ev).status_code == 200
        assert c.post(f"/jobs/{jid}/events", json={**ev, "worker": "intruder"}).status_code == 409
        assert c.post(f"/jobs/{jid}/events", json={**ev, "kind": "job.succeeded"}).status_code == 422
        kinds = [e["kind"] for e in c.get(f"/jobs/{jid}/events").json()]
        assert "model.request" in kinds


def test_build_providers_skips_missing_claude():
    cfg = {"tiers": {"T1": {"provider": "ollama", "model": "m"}, "T3": {"provider": "claude", "model": None}},
           "ollama": {"url": "http://x:1"}, "claude": {"command": ["no-such-claude-cli"], "args": []}}
    p = build_providers(cfg, ollama_url="http://127.0.0.1:11434")
    assert set(p) == {"T1"} and p["T1"].url == "http://127.0.0.1:11434"


# ------------------------------------------------------------------ end to end: worker + argusd + fake Ollama


def test_escalation_end_to_end_on_a_real_server(tmp_path):
    import argus.worker.demo  # noqa: F401

    with FakeOllama({"qwen2.5-coder:7b": [WRONG], "qwen2.5-coder:14b": [GOOD]}) as ol:
        argus = make(tmp_path).open()
        with Server(argus) as srv:
            cl = client(srv.url)
            w = Worker(cl, "gpu-worker", ollama_url=ol.url)
            w.register()
            assert set(w.providers) >= {"T1", "T2"}
            body = {"plugin": "demo", "workflow": "classify", "input": {"filename": "lecture_07.pdf"}}
            jid = cl.post("/jobs", body)["id"]
            assert w.run_once(wait=2)
            job = cl.get(f"/jobs/{jid}")
            assert job["state"] == "succeeded", job
            assert job["result"]["category"] == "Documents" and job["result"]["tier"] == "T2"
            assert job["steps"][0]["tier_used"] == "T2"  # the step records the tier that answered
            kinds = [e["kind"] for e in cl.get(f"/jobs/{jid}/events")]
            assert "check.failed" in kinds and "model.escalated" in kinds and kinds[-1] == "job.succeeded"
            edges = {(e["src"], e["dst"]) for e in cl.get("/map")["edges"]}
            assert {("demo", "t1"), ("t1", "t2"), ("t2", "demo")} <= edges  # the escalation shows on the map
            models = {t["tier"]: t for t in cl.get("/models")["tiers"]}
            assert models["T1"]["calls"] == 2 and models["T2"]["calls"] == 1


def test_step_checkpoint_means_no_second_model_call_on_retry(tmp_path):
    reg = WorkflowRegistry()
    calls = {"n": 0}

    @workflow("p", "w", registry=reg)
    def wf(ctx):
        a = ctx.step("ask", lambda: ctx.llm("sort", "x", schema=Placement).model_dump())
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("fail after the model answered")
        return a

    with FakeOllama({"qwen2.5-coder:7b": [GOOD]}) as ol:
        argus = make(tmp_path).open()
        with Server(argus) as srv:
            cl = client(srv.url)
            w = Worker(cl, "w", registry=reg, ollama_url=ol.url)
            w.register()
            jid = cl.post("/jobs", {"plugin": "p", "workflow": "w", "max_attempts": 3})["id"]
            assert w.run_once(wait=2)
            assert cl.get(f"/jobs/{jid}")["state"] == "retry"
            argus.store.write_sync(lambda c: c.execute("UPDATE jobs SET run_after = 0 WHERE id = ?", (jid,)))
            assert w.run_once(wait=2)
            assert cl.get(f"/jobs/{jid}")["state"] == "succeeded"
            assert len(ol.requests) == 1  # the answer came from the checkpoint the second time



def test_doctor_reports_setup(tmp_path, capsys):
    from argus.models.doctor import main as doctor

    cmd = fake_claude(tmp_path, result='{"ok": true}')
    with FakeOllama({"qwen2.5-coder:7b": [{"ok": True}]}) as ol:
        (tmp_path / "argus.yaml").write_text(
            f"ollama: {{url: '{ol.url}'}}\n"
            "models:\n  tiers:\n    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n"
            "    T2: {provider: ollama, model: 'qwen2.5-coder:14b'}\n    T3: {provider: claude}\n"
            f"claude: {{command: {json.dumps(cmd)}}}\n", encoding="utf-8")
        assert doctor(["--config", str(tmp_path / "argus.yaml"), "--claude"]) == 1
        out = capsys.readouterr().out
        assert "[ok] T1 answered valid JSON" in out and "ollama pull qwen2.5-coder:14b" in out
        assert "[ok] real call with tools off" in out and "1 problem(s)" in out


def test_claude_timeout_ends_the_whole_process_tree(tmp_path):
    """claude.cmd starts node: a timeout must end the child too, or its open pipes keep the worker waiting."""
    import subprocess as sp
    import sys

    from argus.models.providers import run_with_timeout

    script = tmp_path / "parent.py"
    script.write_text("import subprocess, sys, time\n"
                      "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                      "time.sleep(30)\n", encoding="utf-8")
    t0 = time.perf_counter()
    with pytest.raises(sp.TimeoutExpired):
        run_with_timeout([sys.executable, str(script)], "", 0.5)
    assert time.perf_counter() - t0 < 8


def test_router_asks_for_a_permit_per_call():
    """Each call to a tier needs its own permit, so a retry counts against the Claude cap too."""
    with FakeOllama({"t1": [WRONG, GOOD]}) as ol:
        b = FakeBoard()
        calls = []
        orig = b.permit
        b.permit = lambda tier: calls.append(tier) or orig(tier)
        Router(providers(ol), b, chain=["T1"]).ask("sort", "x", schema=Placement, check=check)
        assert calls == ["T1", "T1"]
