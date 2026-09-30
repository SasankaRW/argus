"""Ari thinking: tools one step at a time, plugin tools as child jobs (the thinking job waits for them), local
first, Claude with web search for current things."""

from __future__ import annotations

import json
from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from fakes import FakeOllama, fake_claude
from test_worker import Server, client, wait_for

PLUGIN_YAML = """id: echo-tool
name: Echo tool
version: 1.0.0
kind: workflow
argus_api: ">=1.0 <2.0"
runs_on: desktop
needs: [session]
permissions: {models: []}
ari:
  tools:
    - {name: shout, description: Say something loudly on the PC., workflow: say,
       input: {text: {type: string, description: what to say}}, required: [text]}
    - {name: wipe, description: Delete everything., workflow: say, risky: true}
"""

PLUGIN_PY = """from argus.worker import workflow

@workflow("echo-tool", "say")
def say(ctx):
    return {"said": str(ctx.input.get("text", "")).upper(), "parent": ctx.input.get("_parent")}
"""


def make(tmp_path: Path, extra: str = "") -> Argus:
    pdir = tmp_path / "plugins" / "echo-tool"
    pdir.mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(PLUGIN_YAML)
    (pdir / "plugin.py").write_text(PLUGIN_PY)
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "models:\n  tiers:\n    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n"
        + (extra or "  chain: [T1]\n") +
        f"plugins:\n  dirs: ['{(tmp_path / 'plugins').as_posix()}']\n  live: [echo-tool]\n", encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def settle(cl, w, conv, n=6):
    """Run jobs until Ari's last turn has text."""
    for _ in range(n):
        last = cl.get(f"/ari/{conv}")["turns"][-1]
        if last["text"]:
            return last
        w.run_once(wait=1)
    return wait_for(lambda: (x := cl.get(f"/ari/{conv}")["turns"][-1])["text"] and x)


def test_builtin_then_plugin_tool_then_the_answer(tmp_path):
    replies = {"qwen2.5-coder:7b": [
        {"tool": "argus_status", "args": {}},
        {"tool": "shout", "args": {"text": "hello"}},
        {"reply": "Argus is fine, and I shouted HELLO."}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        tools = {t["name"]: t for t in cl.get("/tools")}
        assert tools["shout"]["plugin"] == "echo-tool" and tools["wipe"]["risky"] is True
        r = cl.post("/ari", {"text": "how is argus, and shout hello"})
        last = settle(cl, w, r["conv"])
        assert last["text"] == "Argus is fine, and I shouted HELLO."
        think = cl.get(f"/jobs/{r['job_id']}")
        names = [s["name"] for s in think["steps"]]
        assert names == ["think 1", "tool 1: argus_status", "think 2", "tool 2: shout", "think 3"]
        child = cl.get("/jobs?plugin=echo-tool")[0]
        assert child["result"] == {"said": "HELLO", "parent": r["job_id"]}
        third = json.loads(ol.requests[2]["messages"][1]["content"])
        assert [d["tool"] for d in third["results_so_far"]] == ["argus_status", "shout"]
        assert third["results_so_far"][1]["result"]["said"] == "HELLO"


def test_risky_tool_asks_and_a_made_up_tool_is_refused(tmp_path):
    replies = {"qwen2.5-coder:7b": [{"tool": "format_disk", "args": {}}, {"tool": "wipe", "args": {}}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "wipe it all"})
        last = settle(cl, w, r["conv"])
        assert last["text"] == "Shall I delete everything?" and last["pending"]["name"] == "wipe"
        assert cl.get("/jobs?plugin=echo-tool") == []  # not run
        assert "not one of the tools" in ol.requests[1]["messages"][-1]["content"]
        no = cl.post("/ari", {"text": "no", "conv": r["conv"]})
        assert no["reply"] == "Okay, I won't." and cl.get("/jobs?plugin=echo-tool") == []


def test_current_things_go_to_claude_with_web_search(tmp_path):
    cmd = fake_claude(tmp_path, result="Sri Lanka won by 5 wickets, says ESPNcricinfo.")
    extra = "    T3: {provider: claude}\n  chain: [T1, T3]\n" + f"claude:\n  command: {json.dumps(cmd)}\n"
    replies = {"qwen2.5-coder:7b": [{"need_web": True}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path, extra).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "who won the cricket yesterday?"})
        last = settle(cl, w, r["conv"])
    assert last["text"] == "Sri Lanka won by 5 wickets, says ESPNcricinfo."
    argv = json.loads((tmp_path / "claude_argv.json").read_text())
    assert "WebSearch,WebFetch" in argv and "--disallowedTools" not in argv
    assert "who won the cricket" in (tmp_path / "claude_stdin.txt").read_text()
