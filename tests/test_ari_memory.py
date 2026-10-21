"""Ari's memory over time: a running summary of a chat's earlier turns, and episodes of finished chats that come
back with a matching message (none for private chats)."""

from __future__ import annotations

import json
import time

from argus import ari
from argus.worker import Worker
from argus.worker.recall import made_up_names
from conftest import run
from fakes import FakeOllama
from test_ari_think import make as make_think
from test_ari_think import settle
from test_worker import Server, client


def _turns(a, conv, said, at):
    """Add turns ("you: ..." / "ari: ...") at time `at` and after, one second apart."""
    def add(c):
        for i, line in enumerate(said):
            role, text = line.split(": ", 1)
            tid = ari.add_turn(c, conv, role, text)
            c.execute("UPDATE ari_turns SET created_at = ? WHERE id = ?", (at + i, tid))
    run(a.store.write(add))


def test_turns_that_fall_out_of_the_window_are_summarized_once_there_are_enough(tmp_path, monkeypatch):
    a = make_think(tmp_path).open()
    try:
        now = time.time()
        monkeypatch.setattr(ari, "day_start", lambda t: now - 6 * 3600)  # today began 6 hours ago, whenever this runs
        start = now - 5 * 3600
        _turns(a, "c1", ["you: open spotify", "ari: Opening Spotify.", "you: play Famous", "ari: Playing Famous.",
                         "you: louder", "ari: Turned it up."], start)
        _turns(a, "c1", ["you: what's the weather", "ari: Sunny."], now - 60)
        todo = run(a.store.read(lambda c: ari.to_summarize(c, "c1", now)))
        assert [t["said"] for t in todo["turns"]][:2] == ["open spotify", "Opening Spotify."]
        assert len(todo["turns"]) == 6 and todo["summary"] == ""  # the last half hour stays word for word
        run(a.store.write(lambda c: ari.keep_summary(c, "c1", "Played Famous on Spotify, louder.", todo["upto"])))
        assert run(a.store.read(lambda c: ari.to_summarize(c, "c1", now))) is None  # nothing new to fold in
        assert run(a.store.read(lambda c: ari.earlier_today(c, "c1", now))) == "Played Famous on Spotify, louder."
        run(a.store.write(lambda c: ari.keep_summary(c, "c1", "older", 1)))  # a late, older summary never wins
        assert run(a.store.read(lambda c: ari.earlier_today(c, "c1", now))) == "Played Famous on Spotify, louder."
    finally:
        a.store.close()


def test_quiet_chats_become_episodes_that_come_back_with_a_matching_message(tmp_path):
    a = make_think(tmp_path).open()
    try:
        now = time.time()
        _turns(a, "old", ["you: play Famous on spotify", "ari: The search box never showed up.",
                          "you: try again later", "ari: Okay."], now - 3600)
        _turns(a, "fresh", ["you: hi", "ari: Hey!", "you: how are you", "ari: Good."], now - 120)
        ended = run(a.store.read(lambda c: ari.ended_chats(c, now)))
        assert [e["conv"] for e in ended] == ["old"]  # "fresh" isn't quiet for 10 minutes yet
        e = ended[0]
        run(a.store.write(lambda c: ari.keep_episode(c, "old", "Wanted Famous on Spotify; the search box never "
                                                     "showed up.", "play Famous", e["started"], e["ended"])))
        assert run(a.store.read(lambda c: ari.ended_chats(c, now))) == []  # written: not again
        got = run(a.store.read(lambda c: ari.episodes_like(c, "play something on spotify", now, not_conv="new")))
        assert got[0]["what"].startswith("Wanted Famous") and got[0]["left_open"] == "play Famous"
        assert got[0]["when"].split()[0] in ("today", "yesterday")  # an hour ago (yesterday just after 4 am)
        assert run(a.store.read(lambda c: ari.episodes_like(c, "spotify", now, not_conv="old"))) == []
        mem = run(a.store.read(lambda c: ari.search_memory(c, "spotify search box", now)))
        assert mem["past_chats"][0]["what"].startswith("Wanted Famous")
    finally:
        a.store.close()


def test_private_chats_are_never_summarized_or_remembered(tmp_path):
    a = make_think(tmp_path).open()
    try:
        now = time.time()

        def add(c):
            c.execute("INSERT INTO jobs (id, plugin, workflow, input, state, priority, attempt, max_attempts, needs,"
                      " run_after, created_at, updated_at, result) VALUES"
                      " ('j1','ari','think','{}','succeeded',90,1,1,'[]',?,?,?,?)",
                      (now, now, now, json.dumps({"used": [{"tool": "read_email", "private": True}]})))
            for i, (role, text) in enumerate([("you", "read my mail"), ("ari", "Kaancha says hi."),
                                              ("you", "and the next"), ("ari", "GitHub.")]):
                tid = ari.add_turn(c, "p", role, text, job_id="j1" if role == "ari" else None)
                c.execute("UPDATE ari_turns SET created_at = ? WHERE id = ?", (now - 3600 + i, tid))
        run(a.store.write(add))
        assert run(a.store.read(lambda c: ari.private_chat(c, "p")))
        assert run(a.store.read(lambda c: ari.ended_chats(c, now))) == []
    finally:
        a.store.close()


def test_the_summary_and_past_chats_reach_ari_and_the_jobs_keep_what_they_write(tmp_path):
    replies = {"qwen2.5-coder:7b": [
        {"reply": "Hello again."},  # the think job
        {"episode": "Wanted Famous on Spotify; it played.", "left_open": ""},  # an episode job
    ]}
    a = make_think(tmp_path).open()
    with FakeOllama(replies) as ol, Server(a) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop", "session"], ollama_url=ol.url, watch_folders=False)
        w.register()
        now = time.time()
        run(a.store.write(lambda c: ari.keep_summary(c, "c9", "Played Famous on Spotify earlier.", 0)))
        _turns(a, "gone", ["you: play Famous on spotify", "ari: Playing it.", "you: louder", "ari: Done."],
               now - 3600)
        r = cl.post("/ari", {"text": "spotify again please", "conv": "c9"})
        settle(cl, w, r["conv"])
        asked = json.loads(ol.requests[0]["messages"][1]["content"])
        assert asked["earlier_today"] == "Played Famous on Spotify earlier."
        ep = [j for j in cl.get("/jobs?plugin=ari") if j["workflow"] == "episode"]
        assert len(ep) == 1 and ep[0]["input"]["conv"] == "gone"
        for _ in range(5):
            w.run_once(wait=1)
        eps = cl.get("/ari-memory/episodes")
        assert eps and eps[0]["text"] == "Wanted Famous on Spotify; it played."
        r2 = cl.post("/ari", {"text": "play famous on spotify", "conv": "c10"})
        assert r2["job_id"]
        job = cl.get(f"/jobs/{r2['job_id']}")
        assert job["input"]["before"][0]["what"].startswith("Wanted Famous")


def test_made_up_names_are_caught():
    turns = [{"who": "you", "said": "open spotify and play Famous"}]
    assert made_up_names("You played Famous on Spotify with Nimali.", turns) == ["Nimali"]
    assert made_up_names("Played Famous. Then it stopped.", turns) == []
