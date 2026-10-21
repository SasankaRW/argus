"""Smarter tool choice: tools picked by meaning (the local embedding model) and the app in use; a request with
several steps planned by Claude first (text only), then done by the local model."""

from __future__ import annotations

import json

from argus.worker import Worker
from argus.worker import think as tk
from fakes import FakeOllama, fake_claude
from test_ari_think import make, settle
from test_worker import Server, client


def _tools(n=30):
    tools = {f"tool_{i}": {"does": f"thing number {i}"} for i in range(n)}
    tools["wake_desk"] = {"does": "Bring the computer out of sleep"}
    tools["play_on_spotify"] = {"does": "Play a song, artist or playlist on Spotify"}
    return tools


def test_meaning_finds_a_tool_that_shares_no_words():
    tools = _tools()
    sims = {n: 0.2 for n in tools} | {"wake_desk": 0.82}
    picked = tk.closest(tools, "rouse my machine", sims=sims)
    assert "wake_desk" in picked
    assert "wake_desk" not in tk.closest(tools, "rouse my machine")  # by words alone it isn't found


def test_the_app_in_use_keeps_its_tools():
    picked = tk.closest(_tools(), "next one", app="Spotify music media song volume")
    assert "play_on_spotify" in picked


def test_meaning_is_cached_and_backs_off_without_the_model(monkeypatch):
    tk._TOOL_VECS.clear()
    tk._NO_EMBED["until"] = 0.0
    calls = []

    def embed(texts):
        calls.append(len(texts))
        return [[1.0, 0.0] if "computer" in t or "machine" in t else [0.0, 1.0] for t in texts]

    tools = _tools()
    sims = tk.meaning(embed, tools, "rouse my machine")
    assert sims["wake_desk"] > 0.99 and sims["tool_1"] < 0.01
    tk.meaning(embed, tools, "again")
    assert calls == [len(tools), 1, 1]  # the tools are embedded once, then only the message
    assert tk.meaning(embed, {"a": {}}, "few tools") is None  # a handful are all offered anyway
    tk._TOOL_VECS.clear()
    assert tk.meaning(lambda texts: None, tools, "x") is None  # not pulled: words only ...
    assert tk.meaning(embed, tools, "x") is None  # ... for a while, without asking again
    tk._NO_EMBED["until"] = 0.0


def test_what_counts_as_several_steps():
    assert tk.multi_step("find the invoice and then open it please")
    assert tk.multi_step("open spotify and play some coldplay for me")
    assert not tk.multi_step("open spotify")
    assert not tk.multi_step("how is argus, and shout hello")


def test_claude_plans_and_the_local_model_does_the_steps(tmp_path):
    plan = {"steps": [{"tool": "shout", "args": {"text": "first"}, "why": "say it"},
                      {"tool": "shout", "args": {"text": "second"}, "why": "say it again"}]}
    cmd = fake_claude(tmp_path, result=json.dumps(plan))
    extra = "    T3: {provider: claude}\n  chain: [T1, T3]\n" + f"claude:\n  command: {json.dumps(cmd)}\n"
    replies = {"qwen2.5-coder:7b": [{"tool": "shout", "args": {"text": "first"}},
                                    {"tool": "shout", "args": {"text": "second"}},
                                    {"reply": "Shouted both."}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path, extra).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "shout first and then shout second, then close it"})
        last = settle(cl, w, r["conv"], n=12)
        assert last["text"] == "Shouted both."
        sent = (tmp_path / "claude_stdin.txt").read_text()
        assert "shout first and then shout second" in sent and "conversation_so_far" not in sent
        task = json.loads(ol.requests[0]["messages"][1]["content"])
        assert [st["args"]["text"] for st in task["plan"]] == ["first", "second"]
        steps = [s["name"] for s in cl.get(f"/jobs/{r['job_id']}")["steps"]]
        assert steps[0] == "plan" and "tool 1: shout" in steps


def test_without_claude_there_is_no_plan_and_ari_still_answers(tmp_path):
    replies = {"qwen2.5-coder:7b": [{"tool": "shout", "args": {"text": "a"}}, {"reply": "Done."}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.post("/ari", {"text": "shout a and then shout b after that"})
        assert settle(cl, w, r["conv"], n=12)["text"] == "Done."
        assert "plan" not in json.loads(ol.requests[0]["messages"][1]["content"])
