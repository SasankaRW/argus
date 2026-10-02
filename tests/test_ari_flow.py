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

    # streamed: the finished sentences go out while the rest is still being written
    with FakeOllama({"qwen2.5-coder:7b": [{"mood": "calm", "reply": "Long day, huh? Put your feet up. I'll keep "
                                                                       "the lights on."}]}) as ol, \
            Server(make(tmp_path / "s").open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "I'm so tired"})
        settle(cl, w, r["conv"])
        evs = cl.get(f"/events?kinds=plugin.ari.partial&job={r['job_id']}")["events"]
        assert [e["data"]["text"] for e in evs] == ["Long day, huh?", "Long day, huh? Put your feet up."]
        assert evs[0]["data"]["mood"] == "calm" and ol.requests[0]["stream"] is True


def test_old_chats_and_old_questions_dont_leak(tmp_path):
    import os
    import sqlite3
    import time as _t

    from argus import ari as ari_mod
    from argus.ari_listen import AriClient
    from argus.db.store import available_migrations

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    for _, _, path in available_migrations():
        c.executescript(path.read_text(encoding="utf-8"))
    ari_mod.add_turn(c, "x", "you", "type hello in the window in front")
    t = ari_mod.add_turn(c, "x", "ari", "Shall I type it?", pending={"kind": "tool", "name": "type_text"})
    c.execute("UPDATE ari_turns SET created_at = created_at - 3600")
    assert ari_mod.open_question(c, "x")["turn"] == t
    assert ari_mod.open_question(c, "x", max_age=600) is None  # an hour later, "yes" isn't about that
    ari_mod.add_turn(c, "x", "you", "what time is it")
    assert [h["text"] for h in ari_mod.history(c, "x")] == ["what time is it"]  # the old chat isn't sent along

    conv_file = tmp_path / "c.conv"
    conv_file.write_text("old-chat")
    old = _t.time() - 7200
    os.utime(conv_file, (old, old))
    cl = AriClient("http://127.0.0.1:9", None, conv_file)
    assert cl.conv == "old-chat" and _t.time() - cl.used > cl.NEW_CHAT_S  # the next message starts a new chat


def test_pausing_ari_s_ears(tmp_path):
    from argus import ari_listen

    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        assert cl.get("/ari/listening") == {"listening": True, "until": None}
        r = cl.post("/ari/listening", {"on": False, "minutes": 3})
        assert r["listening"] is False and r["until"] > 0
        assert cl.post("/ari/listening", {"on": False})["until"] is None  # until turned back on
        evs = cl.get("/events?kinds=ari.listening&after=0")["events"]
        ari_listen.hush_from(evs)
        assert ari_listen.paused()
        assert cl.post("/ari/listening", {"on": True}) == {"listening": True, "until": None}
        ari_listen.hush_from(cl.get("/events?kinds=ari.listening&after=0")["events"])
        assert not ari_listen.paused()


def test_times_said_in_words_like_a_transcript_writes_them():
    import time as _t

    from argus.ari import digits, parse_when

    now = _t.mktime((2026, 10, 1, 15, 0, 0, 0, 0, -1))  # a fixed afternoon: "tonight" must not depend on the clock
    assert parse_when("remind me to call mum tomorrow at five pm", now).say == "tomorrow at 5 pm"
    assert parse_when("sort downloads every morning at seven", now).say == "every day at 7 am"
    assert parse_when("shut down the PC at eleven thirty tonight", now).say.endswith("at 11:30 pm")
    assert parse_when("every thirty minutes check the lab", now).say == "every 30 minutes"
    assert parse_when("remind me in half an hour to stretch", now) is not None
    assert digits("at five oh five pm") == "at 5:05 pm"


def test_typing_tools_only_when_asked_and_whatsapp_goes_straight():
    t = {n: {} for n in ("type_text", "press_keys", "whatsapp_message", "open_app")}
    assert set(offered(t, "open whatsapp", [{"text": "type hello"}])) == {"whatsapp_message", "open_app"}
    assert "type_text" in offered(t, "type hello world", [])
    assert straight_to(t, "Kaancha hi on WhatsApp") == ("whatsapp_message", {"to": "Kaancha", "text": "hi"})


def test_talk_said_mid_sentence_is_still_talk_and_ari_never_repeats_itself():
    from argus.worker.think import _same, chatty

    for t in ("How's it doing", "Just wanna talk with you, I'm tired", "Never mind, thank you", "ugh I'm so tired"):
        assert chatty(t), t
    assert not chatty("I'm tired, set a timer for 20 minutes") and not chatty("what's on my screen")
    said = ["Sorry about that! I'll make sure to use the correct tool next time. How can I assist you?"]
    assert _same("Sorry about that! I'll make sure to use the correct tool next time. How can I assist you now?", said)
    assert not _same("Long day, huh? Want to talk about it?", said)


def test_no_made_up_searches_or_answers_after_a_failed_tool():
    from argus.worker.think import FAILED, _about

    assert not _about("best budget laptop for college students 2025", "can you turn down my volume on my phone")
    assert _about("weather Kandy", "what's the weather in kandy?")
    assert _about("python release", "latest Python release?")
    assert FAILED.search("Sorry, I couldn't look that up right now.")
    assert not FAILED.search("Based on the latest information, the best budget laptops are...")


def test_with_many_tools_the_ones_that_fit_are_offered():
    from argus.worker.think import closest

    tools = {f"tool_{i}": {"description": f"does thing number {i}"} for i in range(40)}
    tools |= {"phone_torch": {"description": "Turn the phone's torch (flashlight) on or off"},
              "set_volume": {"description": "Set the PC's volume"}, "web_search": {"description": "Search the web"}}
    got = closest(tools, "can you turn on the flashlight on my phone")
    assert "phone_torch" in got and "web_search" in got and "tool_3" not in got
    assert list(closest(tools, "zzz qqq")) == ["web_search"]  # nothing fits: the always-useful ones, not all 40
    # (all of them was a prompt too long for the model's context: Ollama cut it and the question was lost)


def test_ari_has_a_personality_and_no_help_desk_lines():
    from argus.worker.think import PERSONA, no_helpdesk, persona

    assert no_helpdesk("Done, Spotify's up. How can I assist you today?") == "Done, Spotify's up."
    assert no_helpdesk("Opened it. Let me know if you need anything else!") == "Opened it."
    assert no_helpdesk("How can I help you?") == "How can I help you?"  # nothing left: keep it
    assert persona({}) == PERSONA and "Sas" in persona({"call_me": "Sas"})
    assert persona({"personality": "A calm butler."}).startswith("A calm butler.")


def test_common_commands_go_straight_to_their_tool_and_reply_without_a_model():
    from argus.worker.think import said_back

    t = {n: {} for n in ("set_volume", "media_control", "phone_torch", "open_app")}
    assert straight_to(t, "turn the volume down a bit please") == ("set_volume", {"change": "down"})
    assert straight_to(t, "set volume to 45%") == ("set_volume", {"level": 45})
    assert straight_to(t, "Hey Ari, mute") == ("set_volume", {"change": "mute"})
    assert straight_to(t, "next song") == ("media_control", {"action": "next"})
    assert straight_to(t, "pause the music") == ("media_control", {"action": "play_pause"})
    assert straight_to(t, "turn on the torch on my phone") == ("phone_torch", {"state": "on"})
    assert straight_to(t, "turn down my volume on my phone") is None  # the PC's volume tool is not the phone's
    assert said_back("set_volume", {"level": 30}, {"volume": 30}) == "Volume's at 30."
    assert "dry run" in said_back("set_volume", {"change": "up"}, {"dry_run": True})


def test_whisper_loads_from_the_local_cache_first():
    from argus.worker.hear import _load

    calls = []

    class M:
        def __init__(self, name, device, compute_type, local_files_only=False):
            calls.append(local_files_only)
            if local_files_only and name == "new":
                raise RuntimeError("not in the cache")

    _load(M, "small.en", "cpu", "int8")
    assert calls == [True]  # no call to huggingface.co
    _load(M, "new", "cpu", "int8")
    assert calls[1:] == [True, False]  # first time: downloaded


def test_sums_are_worked_out_in_code():
    from argus.worker.think import quick_math

    assert quick_math("what's 15 percent of 2400") == "15% of 2,400 is 360."
    assert quick_math("Hey Ari, what is 12% of 80?") == "12% of 80 is 9.6."
    assert quick_math("what's 1250 times 4") == "1,250 × 4 is 5,000."
    assert quick_math("calculate 10 divided by 4") == "10 ÷ 4 is 2.5."
    assert quick_math("what's 7 / 0") == "Can't divide by zero."
    assert quick_math("what's 2400 minus 15 percent") is None  # not one of the simple shapes: the model answers
    assert quick_math("open brave") is None and quick_math("what's the time") is None


def test_long_prompts_get_room_in_the_context():
    from argus.models.providers import context_for

    assert context_for("short", [{"role": "user", "content": "hi"}]) == 8192  # always the same size: no reloads
    assert context_for("x" * 12000, [{"role": "user", "content": "y" * 9000}]) == 8192
    assert context_for("x" * 30000, []) == 16384  # too long for it: more
    assert context_for("x" * 300000, []) == 32768


def test_type_something_is_asked_first_and_phone_volume_is_said_plainly():
    from argus.worker.think import CANT_PHONE_VOLUME, PHONE_VOLUME, straight_to

    tools = {"type_text": {}, "press_keys": {}}
    assert straight_to(tools, "type hello world") == ("type_text", {"text": "hello world"})
    assert straight_to(tools, 'please type "see you at 5"') == ("type_text", {"text": "see you at 5"})
    for no in ("what type of laptop should I buy", "type of cat is this?", "type this into the box", "type"):
        assert straight_to(tools, no) is None
    assert PHONE_VOLUME.search("turn down my volume on my phone") and PHONE_VOLUME.search("mute my phone")
    assert not PHONE_VOLUME.search("turn the volume down") and "phone's volume" in CANT_PHONE_VOLUME


def test_a_task_answer_is_said_while_it_is_written_unless_it_is_about_to_use_a_tool(tmp_path):
    step = {"tool": "", "args": {}, "need_web": False, "mood": "calm",
            "reply": "A mutex lets one thread in at a time. The rest wait their turn."}
    with FakeOllama({"qwen2.5-coder:7b": [step]}) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "explain what a mutex is in one sentence"})
        settle(cl, w, r["conv"])
        evs = cl.get(f"/events?kinds=plugin.ari.partial&job={r['job_id']}")["events"]
        assert [e["data"]["text"] for e in evs] == ["A mutex lets one thread in at a time."]  # the rest follows
        assert evs[0]["data"]["mood"] == "calm"
    # the same words, but the model wants the web first: nothing of it is said early
    web = {**step, "need_web": True}
    with FakeOllama({"qwen2.5-coder:7b": [web, step]}) as ol, Server(make(tmp_path / "w").open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "explain what a mutex is in one sentence"})
        settle(cl, w, r["conv"])
        assert cl.get(f"/events?kinds=plugin.ari.partial&job={r['job_id']}")["events"] == []


def test_events_can_wait_for_something_new(tmp_path):
    import threading
    import time

    with Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        seq = cl.get("/events?limit=1&newest=true")["seq"]
        t0 = time.monotonic()
        assert cl.get(f"/events?kinds=ari.state&after={seq}&wait=0.4")["events"] == []  # nothing: after the wait
        assert time.monotonic() - t0 >= 0.35
        got = {}

        def poll():
            got["r"] = cl.get(f"/events?kinds=ari.state&after={seq}&wait=10")
            got["t"] = time.monotonic()

        th = threading.Thread(target=poll)
        th.start()
        time.sleep(0.3)
        t1 = time.monotonic()
        cl.post("/ari/state", {"phase": "listening"})
        th.join(5)
        assert [e["data"]["phase"] for e in got["r"]["events"]] == ["listening"]
        assert got["t"] - t1 < 2  # answered when it happened, not after the 10 s
