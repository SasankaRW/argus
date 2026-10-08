"""P4: every task learns as it works. Your fixes count without a click (Undo, Wrong, a file moved back or renamed
again, "no, I meant ..." to Ari), and answers you confirmed or left alone become worked examples for similar inputs
(experience memory); examples that keep misleading are dropped."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from argus import ari as ari_mod
from argus import guidance
from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.followup import find
from argus.worker.workflows import similar, worked_examples
from fakes import FakeOllama
from test_worker import Server, client, wait_for

PLUGIN_YAML = """id: mover
name: Mover
version: 1.0.0
kind: workflow
argus_api: ">=1.0 <2.0"
triggers: [{manual: {workflow: file, label: File}}]
workflows: [file, undo]
permissions:
  files: {read: ["~/In"], write: ["~/In"], delete: none}
  models: [T1]
"""

PLUGIN_PY = '''import os
from pydantic import BaseModel
from argus.worker import workflow

PLAYBOOK = """Pick the folder for a file.
Answer as JSON: {"folder": "<Bills|Photos|Other>"}"""


class Pick(BaseModel):
    folder: str


@workflow("mover", "file")
def file(ctx):
    path = ctx.input["path"]
    name = os.path.basename(path)
    folder = ctx.step("pick", lambda: ctx.llm(PLAYBOOK, {"file": name}, schema=Pick, subject=path).folder)
    return ctx.step("move", ctx.files.move, path, os.path.join(os.path.dirname(path), folder, name))


@workflow("mover", "undo")
def undo(ctx):
    return ctx.step("undo", ctx.files.move, ctx.input["from"], ctx.input["to"])
'''


def make(tmp_path: Path, monkeypatch) -> tuple[Argus, Path]:
    home = tmp_path / "home"
    inbox = home / "In"
    inbox.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    pdir = tmp_path / "plugins" / "mover"
    pdir.mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(PLUGIN_YAML)
    (pdir / "plugin.py").write_text(PLUGIN_PY)
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "models:\n  tiers:\n    T1: {provider: ollama, model: small}\n  chain: [T1]\n  attempts_per_tier: 1\n"
        f"plugins:\n  dirs: ['{(tmp_path / 'plugins').as_posix()}']\n  live: [mover]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml")), inbox


def work(cl, w, jid):
    for _ in range(4):
        w.run_once(wait=1)
        j = cl.get(f"/jobs/{jid}")
        if j["state"] in ("succeeded", "dead"):
            return j
    return wait_for(lambda: (x := cl.get(f"/jobs/{jid}"))["state"] in ("succeeded", "dead") and x)


def drop(inbox: Path, name: str) -> str:
    p = inbox / name
    p.write_text(name * 3)
    return str(p)


def test_your_fixes_count_and_kept_answers_become_examples(tmp_path, monkeypatch):
    argus, inbox = make(tmp_path, monkeypatch)
    monkeypatch.setattr(guidance, "KEEP_AFTER", 0)  # "left alone for a day", at once
    replies = {"small": [{"folder": "Photos"},   # invoice-oct.pdf: wrong (you rename / move it)
                         {"folder": "Bills"},    # invoice-sep.pdf: right (left alone)
                         {"folder": "Other"},    # beach.jpg: you move it back
                         {"folder": "Other"},    # electricity-invoice-nov.pdf: wrong despite the example
                         {"folder": "Bills"}]}
    with FakeOllama(replies) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        assert "mover" in w.plugins, w.plugin_errors
        jobs = {}
        for n in ("invoice-oct.pdf", "invoice-sep.pdf", "beach.jpg"):
            jobs[n] = work(cl, w, cl.post("/jobs", {"plugin": "mover", "workflow": "file",
                                                    "input": {"path": drop(inbox, n)}})["id"])
            assert jobs[n]["state"] == "succeeded", jobs[n]["error"]
        s = {n: cl.get(f"/jobs/{j['id']}/samples")[0] for n, j in jobs.items()}
        assert s["beach.jpg"]["subject"].endswith("beach.jpg")

        # what you do in Explorer: rename one, move one back, leave one
        os.rename(inbox / "Photos" / "invoice-oct.pdf", inbox / "Photos" / "2026-10 invoice.pdf")
        shutil.move(str(inbox / "Other" / "beach.jpg"), str(inbox / "beach.jpg"))
        assert len(cl.post("/guidance/followups")["jobs"]) == 1
        fj = next(j for j in cl.get("/jobs?plugin=guidance") if j["workflow"] == "followup")
        done = work(cl, w, fj["id"])
        assert done["state"] == "succeeded", done["error"]
        assert done["result"]["seen"] == {"renamed": 1, "there": 1, "moved back": 1}
        after = {n: cl.get(f"/jobs/{j['id']}/samples")[0] for n, j in jobs.items()}
        assert (after["invoice-oct.pdf"]["verdict"], after["invoice-oct.pdf"]["feedback"]) == ("wrong", "renamed")
        assert after["invoice-oct.pdf"]["correction"] == "you renamed it to 2026-10 invoice.pdf"
        assert (after["beach.jpg"]["verdict"], after["beach.jpg"]["feedback"]) == ("wrong", "moved back")
        assert after["invoice-sep.pdf"]["verdict"] is None and after["invoice-sep.pdf"]["kept_at"]
        assert cl.get("/guidance?plugin=mover")[0]["to_review"] == 0  # wrong ones count as mistakes, not this
        assert len(guidance.review_inputs(_conn(argus))[0]["mistakes"]) == 2

        # a similar file: the kept answer and your fix come with it as worked examples
        j4 = work(cl, w, cl.post("/jobs", {"plugin": "mover", "workflow": "file",
                                           "input": {"path": drop(inbox, "invoice-nov.pdf")}})["id"])
        system = ol.requests[-1]["messages"][0]["content"]
        assert "Worked examples" in system and '"folder": "Bills"' in system
        assert "you renamed it to 2026-10 invoice.pdf" in system
        s4 = cl.get(f"/jobs/{j4['id']}/samples")[0]
        assert set(s4["examples_used"]) == {after["invoice-sep.pdf"]["id"], after["invoice-oct.pdf"]["id"]}
        # it was wrong anyway: the examples it was shown get a strike; two strikes and a kept one is dropped
        cl.post(f"/samples/{s4['id']}/verdict", {"verdict": "wrong"})
        cl.post(f"/samples/{s4['id']}/verdict", {"verdict": None})
        cl.post(f"/samples/{s4['id']}/verdict", {"verdict": "wrong"})
        ex = guidance.examples(_conn(argus), "mover")
        assert after["invoice-sep.pdf"]["id"] not in [e["id"] for e in next(iter(ex.values()))]


def _conn(argus: Argus) -> sqlite3.Connection:
    c = sqlite3.connect(argus.cfg.db_path, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def _db(tmp_path: Path) -> sqlite3.Connection:
    from argus.db.store import Store

    path = tmp_path / "t.db"
    Store(path).open().close()
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def test_undo_counts_as_wrong(tmp_path, monkeypatch):
    argus, inbox = make(tmp_path, monkeypatch)
    with FakeOllama({"small": [{"folder": "Photos"}]}) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        j = work(cl, w, cl.post("/jobs", {"plugin": "mover", "workflow": "file",
                                          "input": {"path": drop(inbox, "bill.pdf")}})["id"])
        ch = cl.get(f"/jobs/{j['id']}/changes")[0]
        u = cl.post(f"/jobs/{j['id']}/changes/{ch['event_id']}/undo")
        assert work(cl, w, u["id"])["state"] == "succeeded"
        s = cl.get(f"/jobs/{j['id']}/samples")[0]
        assert (s["verdict"], s["feedback"], s["correction"]) == ("wrong", "undo", "you undid it")
        assert (inbox / "bill.pdf").exists()
        # your own verdict is never overridden
        cl.post(f"/samples/{s['id']}/verdict", {"verdict": "correct"})
        with _conn(argus) as c:
            assert guidance.implicit(c, j["id"], str(inbox / "bill.pdf"), "wrong", "x", "undo") == []


def test_no_i_meant_marks_the_last_answer_wrong(tmp_path):
    conn = _db(tmp_path)
    guidance.add_sample(conn, 1.0, job_id="J1", plugin="ari", playbook="Pick a tool.", schema=None,
                        input="play some jazz", output={"tool": "open_app"}, tier="T1", escalated=False, keep=50)
    ari_mod.add_turn(conn, "c1", "you", "play some jazz")
    ari_mod.add_turn(conn, "c1", "ari", "Opening Jazz.", job_id="J1")
    assert ari_mod.CORRECTION.match("no, I meant on Spotify")
    assert not ari_mod.CORRECTION.match("no worries") and not ari_mod.CORRECTION.match("nothing else")
    assert ari_mod.mark_corrected(conn, "c1", "no, I meant on Spotify", time.time()) != []
    r = conn.execute("SELECT verdict, feedback, correction FROM samples").fetchone()
    assert (r["verdict"], r["feedback"]) == ("wrong", "ari") and "on Spotify" in r["correction"]
    assert ari_mod.mark_corrected(conn, "c1", "no, I meant X", time.time() + 3600) == []  # too late to be about it


def test_the_most_similar_examples_are_picked():
    ex = [{"id": 1, "input": {"file": "invoice-sep.pdf"}, "answer": {"folder": "Bills"}},
          {"id": 2, "input": {"file": "beach.jpg"}, "answer": {"folder": "Photos"}},
          {"id": 3, "input": {"file": "electricity invoice.pdf"}, "answer": "you moved it to Bills", "fixed": True}]
    got = similar(ex, {"file": "invoice-oct.pdf"})
    assert [e["id"] for e in got] == [1, 3]
    assert similar(ex, "") == [] and similar([], "x") == []
    text = worked_examples(got)
    assert text.startswith("Worked examples") and 'Right answer: {"folder": "Bills"}' in text
    assert "What the user did to fix the answer: you moved it to Bills" in text


def test_finding_a_file_you_moved(tmp_path):
    (tmp_path / "Bills").mkdir()
    (tmp_path / "Other").mkdir()
    a = tmp_path / "Bills" / "a.pdf"
    a.write_text("hello")
    st = a.stat()
    assert find(str(a), str(tmp_path / "a.pdf"), st.st_size, st.st_mtime) == ("there", str(a))
    b = tmp_path / "Other" / "a.pdf"
    os.rename(a, b)
    assert find(str(a), str(tmp_path / "a.pdf"), st.st_size, st.st_mtime) == ("moved", str(b))
    os.rename(b, tmp_path / "a.pdf")
    assert find(str(a), str(tmp_path / "a.pdf"), st.st_size, st.st_mtime)[0] == "moved back"
    os.remove(tmp_path / "a.pdf")
    assert find(str(a), str(tmp_path / "a.pdf"), st.st_size, st.st_mtime) == ("gone", None)


@pytest.mark.parametrize("text", ["no, I meant the other one", "that's wrong", "not that one", "I meant tomorrow"])
def test_corrections_are_recognised(text):
    assert ari_mod.CORRECTION.match(text)


def test_sample_json_shows_feedback(tmp_path):
    conn = _db(tmp_path)
    sid = guidance.add_sample(conn, 1.0, job_id="J", plugin="p", playbook="x", schema=None, input="i", output="o",
                              tier="T1", escalated=False, keep=50, subject="/a/b.pdf", used=[7])
    guidance.set_verdict(conn, sid, "wrong", "should be c", feedback="wrong button")
    out = guidance.sample_json(conn.execute("SELECT * FROM samples").fetchone())
    assert out["subject"] == "/a/b.pdf" and out["feedback"] == "wrong button" and out["examples_used"] == [7]
    assert json.dumps(out)
