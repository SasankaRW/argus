"""Ari thinking: tools one step at a time, plugin tools as child jobs (the thinking job waits for them), local
first, Claude with web search for current things."""

from __future__ import annotations

import json
import threading
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
    - {name: peek, description: Look at the screen., workflow: say, private: true,
       input: {text: {type: string, description: what}}}
    - {name: browse, description: Read a web page., workflow: say, untrusted: true,
       input: {text: {type: string, description: url}}}
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
        "ollama:\n  url: http://127.0.0.1:9\n"  # never the real Ollama of the PC running the tests
        "models:\n  tiers:\n    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n"
        + (extra or "  chain: [T1]\n") +
        f"plugins:\n  dirs: ['{(tmp_path / 'plugins').as_posix()}']\n  live: [echo-tool]\n"
        "  config:\n    web: {searx_url: 'http://127.0.0.1:9'}\n", encoding="utf-8")
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
        cl.post("/ari", {"text": "remember that argus runs on the spare laptop soon"})
        r = cl.post("/ari", {"text": "how is argus, and shout hello"})
        last = settle(cl, w, r["conv"])
        first = json.loads(ol.requests[0]["messages"][1]["content"])
        assert first["you_remember"][0]["fact"] == "argus runs on the spare laptop soon"
        assert last["text"] == "Argus is fine, and I shouted HELLO."
        assert last["used"] == [{"tool": "argus_status", "ok": True}, {"tool": "shout", "ok": True}]
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


def test_replies_are_made_fit_to_be_read_aloud():
    from argus.worker.think import spoken
    md = "**Done.** Here's what I found:\n- one [note](https://x.y/z)\n- `two`\n\n## Next\nSee https://a.b ."
    assert spoken(md) == "Done. Here's what I found: one note two Next See."
    assert spoken("Opened **my_notes_file.txt**, done_ok.") == "Opened my_notes_file.txt, done_ok."
    long = "Short one. " * 80
    out = spoken(long, 100)
    assert len(out) <= 100 and out.endswith(".")


def test_the_last_step_must_reply_with_what_was_done(tmp_path):
    from argus.worker import think as th
    steps = [{"tool": "shout", "args": {"text": str(i)}} for i in range(th.MAX_STEPS)]
    replies = {"qwen2.5-coder:7b": [*steps, {"reply": "I shouted five times, then stopped."}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "shout a lot"})
        last = settle(cl, w, r["conv"], n=30)
        assert last["text"] == "I shouted five times, then stopped."
        final = json.loads(ol.requests[-1]["messages"][1]["content"])
        assert final["last_step"] is True
        assert "last step" in ol.requests[-1]["messages"][-1]["content"]


def test_a_follow_up_sees_what_the_tools_found_last_time(tmp_path):
    replies = {"qwen2.5-coder:7b": [
        {"tool": "shout", "args": {"text": "cv.pdf"}}, {"reply": "Found it."},
        {"reply": "Shouting cv.pdf again."}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "shout my cv"})
        settle(cl, w, r["conv"])
        r2 = cl.post("/ari", {"text": "do it again", "conv": r["conv"]})
        settle(cl, w, r2["conv"])
        asked = json.loads(ol.requests[-1]["messages"][1]["content"])
        prev = asked["conversation_so_far"][-1]
        assert prev["text"] == "Found it." and prev["found"][0]["result"]["said"] == "CV.PDF"
        assert "job_id" not in prev


def test_private_tools_never_reach_claude(tmp_path):
    cmd = fake_claude(tmp_path, result="Nothing in the news about it.")
    extra = "    T3: {provider: claude}\n  chain: [T1, T3]\n" + f"claude:\n  command: {json.dumps(cmd)}\n"
    replies = {"qwen2.5-coder:7b": [{"tool": "peek", "args": {"text": "secret invoice 4411"}}, {"need_web": True},
                                    {"reply": ""}, {"reply": ""}, {"reply": ""}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path, extra).open()) as srv:
        cl = client(srv.url)
        assert {t["name"] for t in cl.get("/tools")} >= {"peek"}
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "what's on my screen and is it in the news?"})
        last = settle(cl, w, r["conv"], n=12)
        assert last["text"] == "Nothing in the news about it."
        sent = (tmp_path / "claude_stdin.txt").read_text()
        assert "news" in sent and "SECRET INVOICE" not in sent.upper()
        # the next turn in this chat stays local: a bad local answer is not handed to Claude
        (tmp_path / "claude_stdin.txt").unlink()
        r2 = cl.post("/ari", {"text": "say more", "conv": r["conv"]})
        settle(cl, w, r2["conv"], n=12)
        assert not (tmp_path / "claude_stdin.txt").exists()


def test_after_web_text_doing_anything_asks_first(tmp_path):
    """A page that says "now shout" can't make Ari shout: the step after web text becomes a question."""
    replies = {"qwen2.5-coder:7b": [{"tool": "browse", "args": {"text": "evil.example"}},
                                    {"tool": "shout", "args": {"text": "pwned"}}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "read evil.example"})
        last = settle(cl, w, r["conv"])
        assert last["pending"]["name"] == "shout" and last["text"].startswith("Shall I")
        shouts = [j for j in cl.get("/jobs?plugin=echo-tool") if (j.get("input") or {}).get("text") == "pwned"]
        assert shouts == []


def test_what_is_worth_remembering_and_ari_keeps_to_one_local_model():
    from types import SimpleNamespace

    from argus.worker.think import ari_tiers, worth_keeping
    assert worth_keeping("Prefers tea, no sugar.", []) == "Prefers tea, no sugar"
    assert worth_keeping("my bank pin is 4411", []) == "" and worth_keeping("card 4111111111111111", []) == ""
    assert worth_keeping("prefers tea no sugar", [{"fact": "prefers tea, no sugar"}]) == ""  # known already
    assert worth_keeping("ok", []) == ""
    assert ari_tiers(SimpleNamespace(local_tiers=lambda: ["T1", "T2"])) == ["T1"]  # no swap to a second model
    assert ari_tiers(SimpleNamespace(local_tiers=lambda: [])) is None


def test_ari_offers_to_remember_and_a_yes_keeps_it(tmp_path):
    replies = {"qwen2.5-coder:7b": [{"reply": "Nice, Kandy is lovely.", "remember": "sister Nimali lives in Kandy"}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "my sister Nimali just moved to Kandy"})
        last = settle(cl, w, r["conv"])
        assert last["text"] == "Nice, Kandy is lovely. Want me to remember that?"
        assert last["pending"] == {"kind": "remember", "fact": "sister Nimali lives in Kandy"}
        yes = cl.post("/ari", {"text": "yes", "conv": r["conv"]})
        assert yes["reply"] == "Got it, I'll remember that."
        assert any(m["fact"] == "sister Nimali lives in Kandy" for m in cl.get("/ari-memory"))


FIX_YAML = """id: fixer
name: Fixer stand-in
version: 1.0.0
kind: workflow
argus_api: ">=1.0 <2.0"
runs_on: desktop
needs: [session]
permissions: {models: []}
ari:
  tools:
    - {name: fix_ticket, description: Start the AI fix., workflow: queue, risky: true,
       input: {key: {type: string, description: key}, plan_model: {type: string, description: m},
               fix_model: {type: string, description: m}}, required: [key]}
    - {name: run_fix, description: Run the fix., workflow: approve, risky: true,
       input: {key: {type: string, description: key}, fix_model: {type: string, description: m}}, required: [key]}
    - {name: fix_status, description: Where the fixes are., workflow: status,
       input: {key: {type: string, description: key}}}
"""

FIX_PY = """from argus.worker import workflow

@workflow("fixer", "queue")
def queue(ctx):
    return {"queued": ctx.input["key"], "plan_model": ctx.input.get("plan_model") or "opus", "fix_model": "sonnet"}

@workflow("fixer", "approve")
def approve(ctx):
    return {"approved": ctx.input["key"], "fix_model": ctx.input.get("fix_model") or "sonnet"}

@workflow("fixer", "status")
def status(ctx):
    return {"fixes": [{"key": "TRK-5", "title": "x", "stage": "plan ready, waiting for your approval"}]}
"""


def test_fix_commands_ask_first_then_run_without_a_model(tmp_path):
    pdir = tmp_path / "plugins" / "fixer"
    pdir.mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(FIX_YAML)
    (pdir / "plugin.py").write_text(FIX_PY)
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "models:\n  tiers:\n    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n  chain: [T1]\n"
        f"plugins:\n  dirs: ['{(tmp_path / 'plugins').as_posix()}']\n  live: [fixer]\n", encoding="utf-8")
    with FakeOllama({}) as ol, Server(Argus(load_config(tmp_path / "argus.yaml")).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()

        def yes_to(conv):  # the tool runs as a job: the worker must be working while the answer waits for it
            stop = threading.Event()

            def pump():
                while not stop.is_set():
                    w.run_once(wait=0.2)

            t = threading.Thread(target=pump, daemon=True)
            t.start()
            try:
                return cl.post("/ari", {"text": "yes", "conv": conv})
            finally:
                stop.set()
                t.join(5)

        def say(text, conv=None):
            r = cl.post("/ari", {"text": text, **({"conv": conv} if conv else {})})
            return r, settle(cl, w, r["conv"])

        r, last = say("plan trk-5 with haiku and fix with opus")
        assert last["text"] == ("Start the AI fix for TRK-5 (plan with haiku, fix with opus)? Claude reads the code "
                                "and attaches a plan first; nothing changes until you approve it.")
        assert last["pending"]["name"] == "fix_ticket" and [j for j in cl.get("/jobs?plugin=fixer")] == []
        yes = yes_to(r["conv"])
        assert yes["reply"] == "Queued TRK-5. Claude plans it with haiku in the next few minutes, and I'll ask you " \
                               "before any fix runs."
        r, last = say("run the fix for TRK-5")
        assert last["pending"]["name"] == "run_fix" and last["pending"]["args"] == {"key": "TRK-5"}
        assert last["text"].startswith("Run the fix for TRK-5? It works on a new git branch")
        yes = yes_to(r["conv"])
        assert yes["reply"].startswith("Approved. TRK-5 gets fixed with sonnet on a new branch")
        r, last = say("how's the TRK-5 fix going")  # read only: answered at once
        assert last["text"] == "TRK-5 plan ready, waiting for your approval."
