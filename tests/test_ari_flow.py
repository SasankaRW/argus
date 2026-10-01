"""Ari feels like one flow: the screen and the clipboard only when asked about, and then straight away (no model
deciding first, and the screen's answer is the reply)."""

from __future__ import annotations

from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.think import offered, straight_to
from fakes import FakeOllama
from test_ari_think import settle
from test_worker import Server, client

YAML = """id: screen
name: Screen
version: 1.0.0
kind: workflow
argus_api: ">=1.0 <2.0"
runs_on: desktop
needs: [session]
permissions: {models: []}
ari:
  tools:
    - {name: look_at_screen, description: Look at the screen., workflow: look, private: true,
       input: {question: {type: string, description: what}}}
    - {name: summarise_clipboard, description: Sum up the clipboard., workflow: look, private: true,
       input: {how: {type: string, description: how}}}
"""
PY = """from argus.worker import workflow

@workflow("screen", "look")
def look(ctx):
    return {"answer": "A Python error: KeyError 'name' on line 12.", "asked": ctx.input.get("question")}
"""

TOOLS = {"look_at_screen": {"name": "look_at_screen"}, "summarise_clipboard": {"name": "summarise_clipboard"},
         "weather": {"name": "weather"}}


def test_the_screen_is_only_offered_when_asked_about():
    assert set(offered(TOOLS, "How's the weather", [])) == {"weather"}
    assert "look_at_screen" in offered(TOOLS, "what does this error mean?", [])
    assert "summarise_clipboard" in offered(TOOLS, "sum up what I copied", [])
    assert "look_at_screen" in offered(TOOLS, "and the other one?", [{"text": "what's on my screen?"}])
    assert straight_to(TOOLS, "what's on my screen?") == ("look_at_screen", {"question": "what's on my screen?"})
    assert straight_to(TOOLS, "how's the weather") is None


def make(tmp_path: Path) -> Argus:
    pdir = tmp_path / "plugins" / "screen"
    pdir.mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(YAML)
    (pdir / "plugin.py").write_text(PY)
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "models:\n  tiers:\n    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n  chain: [T1]\n"
        f"plugins:\n  dirs: ['{(tmp_path / 'plugins').as_posix()}']\n  live: [screen]\n", encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def test_whats_on_my_screen_goes_straight_to_the_screen(tmp_path):
    with FakeOllama({"qwen2.5-coder:7b": []}) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "what does this error say?"})
        last = settle(cl, w, r["conv"])
        assert last["text"] == "A Python error: KeyError 'name' on line 12."
        assert ol.requests == []  # no model step before or after: one flow
        names = [s["name"] for s in cl.get(f"/jobs/{r['job_id']}")["steps"]]
        assert names == ["tool 1: look_at_screen"]


def test_open_an_app_needs_no_model():
    t = {"open_app": {"name": "open_app"}}
    assert straight_to(t, "open brave") == ("open_app", {"name": "brave"})
    assert straight_to(t, "Launch VS Code please") == ("open_app", {"name": "VS Code"})
    for no in ["open the file report.pdf", "open it", "start a timer", "open youtube.com", "start the backup"]:
        assert straight_to(t, no) is None, no


def test_whisper_mishearings_are_put_right():
    from argus.vocab import fix, prompt, words

    assert fix("Now, bro, open what's up and say hi to Kancha", {"kancha": "Kaancha"}) == \
        "Now, bro, open WhatsApp and say hi to Kaancha"
    assert fix("what's up with the server?") == "what's up with the server?"
    names = words(["Kaancha"], ["sister Nimali lives in Kandy"])
    assert names[:3] == ["Kaancha", "Nimali", "Kandy"] and "WhatsApp" in prompt(names)


def test_promises_are_not_answers():
    from argus.worker.think import PROMISE, spoken

    assert PROMISE.match("I will open the web page") and PROMISE.match("Okay, I'll send it")
    assert not PROMISE.match("I'll need their number first.") and not PROMISE.match("It's 5 pm.")
    assert spoken("I will open the web page at https://web.whatsapp.com.") == "I will open the web page."



def test_small_talk_is_just_talk(tmp_path):
    from argus.worker.think import chatty

    assert chatty("how are you") and chatty("I'm bored") and chatty("tell me a joke")
    assert not chatty("how's the weather") and not chatty("hi, set a timer for 5 minutes")
    with FakeOllama({"qwen2.5-coder:7b": [{"reply": "Living the dream in your PC. You?"}]}) as ol, \
            Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "how are you doing?"})
        last = settle(cl, w, r["conv"])
        assert last["text"] == "Living the dream in your PC. You?"
        assert "companion" in ol.requests[0]["messages"][0]["content"]  # the chat persona, no tool list
        assert [s["name"] for s in cl.get(f"/jobs/{r['job_id']}")["steps"]] == ["chat"]
