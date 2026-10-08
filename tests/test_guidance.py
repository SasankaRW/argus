"""The guidance loop: answers kept, your verdicts, the nightly review's lessons with before/after scores, your
approval, and the lessons in the playbook from then on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.client import ApiError
from argus.worker.review import same
from fakes import FakeOllama, fake_claude
from test_worker import Server, client, wait_for

PLUGIN_YAML = """id: sorter
name: Sorter
version: 1.0.0
kind: workflow
argus_api: ">=1.0 <2.0"
triggers: [{manual: {workflow: pick, label: Pick}}]
permissions: {models: [T1]}
"""

PLUGIN_PY = '''from pydantic import BaseModel
from argus.worker import workflow

PLAYBOOK = """Pick the folder for a file.
Answer as JSON: {"folder": "<Bills|Photos|Other>"}"""


class Pick(BaseModel):
    folder: str


def check(p, _inp):
    return None if p.folder in ("Bills", "Photos", "Other") else "folder must be Bills, Photos or Other"


@workflow("sorter", "pick")
def pick(ctx):
    return ctx.step("pick", lambda: ctx.llm(PLAYBOOK, ctx.input.get("name", ""), schema=Pick, check=check).folder)
'''


def make(tmp_path: Path) -> Argus:
    pdir = tmp_path / "plugins" / "sorter"
    pdir.mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(PLUGIN_YAML)
    (pdir / "plugin.py").write_text(PLUGIN_PY)
    cmd = fake_claude(tmp_path, result="- An invoice or a bill always goes to Bills.\n- Answer with a folder "
                                        "from the list, spelled exactly.")
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "models:\n  tiers:\n    T1: {provider: ollama, model: small}\n    T2: {provider: ollama, model: big}\n"
        "    T3: {provider: claude}\n  chain: [T1, T2]\n  attempts_per_tier: 1\n"
        f"claude:\n  command: {json.dumps(cmd)}\n"
        f"plugins:\n  dirs: ['{(tmp_path / 'plugins').as_posix()}']\n  live: [sorter]\n", encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def run_job(cl, w, workflow="pick", plugin="sorter", **inp):
    job = cl.post("/jobs", {"plugin": plugin, "workflow": workflow, "input": inp})
    for _ in range(4):
        w.run_once(wait=1)
        j = cl.get(f"/jobs/{job['id']}")
        if j["state"] in ("succeeded", "dead", "waiting"):
            return j
    return wait_for(lambda: (x := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead", "waiting") and x)


def test_matching():
    assert same({"folder": "Bills"}, {"folder": "bills ", "why": "x"})
    assert not same({"folder": "Bills"}, {"folder": "Other"})
    assert same("Hello  world", "hello world") and not same({"a": 1}, None)


def test_the_loop(tmp_path):
    replies = {
        "small": [{"folder": "Invoices"},            # job 1: T1 wrong (not in the list) -> escalated
                  {"folder": "Photos"},              # job 2: T1 right
                  {"folder": "Other"},               # eval before lessons: fails (expected Photos)
                  {"folder": "Photos"},              # eval with lessons: passes
                  {"folder": "Bills"}],              # job 3 (with lessons)
        "big": [{"folder": "Bills"}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        j1 = run_job(cl, w, name="invoice-oct.pdf")
        assert j1["result"] == "Bills"
        s1 = cl.get(f"/jobs/{j1['id']}/samples")
        assert len(s1) == 1 and s1[0]["escalated"] and s1[0]["tier"] == "T2" and s1[0]["output"] == {"folder": "Bills"}
        j2 = run_job(cl, w, name="beach.jpg")
        s2 = cl.get(f"/jobs/{j2['id']}/samples")[0]
        cl.post(f"/samples/{s2['id']}/verdict", {"verdict": "correct"})
        ov = cl.get("/guidance?plugin=sorter")[0]
        assert (ov["samples"], ov["escalated"], ov["correct"], ov["to_review"]) == (2, 1, 1, 1)

        r = cl.post("/guidance/review")
        assert r["playbooks"] == 1
        for _ in range(3):
            w.run_once(wait=1)
        review = wait_for(lambda: (x := cl.get(f"/jobs/{r['job_id']}"))["state"] in ("waiting", "dead") and x)
        assert review["state"] == "waiting", review["error"]
        a = cl.get("/approvals?state=pending")[0]
        assert a["title"] == "New lessons for sorter"
        assert "Tests: 0/1 now, 1/1 with these." in a["payload"]["summary"]
        assert "- An invoice or a bill always goes to Bills." in a["payload"]["summary"]
        runs = cl.get(f"/guidance/{ov['key']}/evals")["runs"]
        assert [(x["passed"], x["total"], x["why"], x["tier"]) for x in runs] == [(0, 1, "review", "T1")]
        proposed = cl.get("/guidance?plugin=sorter")[0]["lessons"]
        assert proposed[0]["state"] == "proposed"
        assert cl.post("/guidance/review")["playbooks"] == 0  # nothing new: no second review
        cl.post(f"/approvals/{a['id']}/decide", {"answer": "approve"})
        wait_for(lambda: cl.get(f"/jobs/{review['id']}")["state"] == "queued")
        w.run_once(wait=1)
        done = wait_for(lambda: (x := cl.get(f"/jobs/{review['id']}"))["state"] == "succeeded" and x)
        assert done["result"]["reviewed"][0]["approved"] is True
        assert cl.get("/guidance?plugin=sorter")[0]["lessons"][0]["state"] == "active"

        j3 = run_job(cl, w, name="bill-nov.pdf")
        assert j3["result"] == "Bills"
        system = ol.requests[-1]["messages"][0]["content"]
        assert "Lessons from earlier mistakes" in system and "invoice or a bill" in system
        # the sample keeps the playbook as written (so it stays one playbook)
        assert len(cl.get("/guidance?plugin=sorter")) == 1
        lesson = cl.get("/guidance?plugin=sorter")[0]["lessons"][0]["id"]
        cl.call("DELETE", f"/lessons/{lesson}")
        assert cl.get("/guidance?plugin=sorter")[0]["lessons"] == []



def work(cl, w, jid, until=("succeeded", "dead")):
    for _ in range(4):
        w.run_once(wait=1)
        j = cl.get(f"/jobs/{jid}")
        if j["state"] in until:
            return j
    return wait_for(lambda: (x := cl.get(f"/jobs/{jid}"))["state"] in until and x)


def test_lessons_that_make_the_tests_worse_are_not_offered(tmp_path):
    replies = {"small": [{"folder": "Invoices"}, {"folder": "Photos"},
                         {"folder": "Photos"},   # tests now: pass
                         {"folder": "Other"}],   # tests with the new lessons: fail
               "big": [{"folder": "Bills"}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        run_job(cl, w, name="invoice-oct.pdf")
        s2 = cl.get(f"/jobs/{run_job(cl, w, name='beach.jpg')['id']}/samples")[0]
        cl.post(f"/samples/{s2['id']}/verdict", {"verdict": "correct"})
        r = cl.post("/guidance/review")
        done = work(cl, w, r["job_id"], until=("succeeded", "dead", "waiting"))
        assert done["state"] == "succeeded", done["error"]
        assert done["result"]["reviewed"][0]["skipped"].startswith("didn't help: 0/1 tests with them, 1/1 without")
        assert cl.get("/approvals?state=pending") == [] and cl.get("/guidance?plugin=sorter")[0]["lessons"] == []


def test_run_the_tests_now_and_try_another_model(tmp_path):
    replies = {"small": [{"folder": "Photos"},   # the job
                         {"folder": "Photos"},   # Run tests now: pass
                         {"folder": "Other"}],   # the second run: fail
               "big": [{"folder": "Photos"}]}    # Try with T2: agrees
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        s = cl.get(f"/jobs/{run_job(cl, w, name='beach.jpg')['id']}/samples")[0]
        key = s["playbook"]
        assert cl.get(f"/guidance/{key}/evals")["runs"] == []
        cl.post(f"/samples/{s['id']}/verdict", {"verdict": "correct"})
        assert [t["id"] for t in cl.get(f"/guidance/{key}/evals")["tests"]] == [s["id"]]
        for want in (1, 0):
            q = cl.post(f"/guidance/{key}/evals")
            assert q["tests"] == 1
            assert work(cl, w, q["job_id"])["result"]["passed"] == want
        assert [(r["passed"], r["why"]) for r in cl.get(f"/guidance/{key}/evals")["runs"]] == [(1, "manual"),
                                                                                             (0, "manual")]
        assert cl.get("/guidance?plugin=sorter")[0]["last_run"]["passed"] == 0
        rep = cl.post(f"/samples/{s['id']}/replay", {"tier": "T2"})
        res = work(cl, w, rep["job_id"])["result"]
        assert res == {"tier": "T2", "answer": {"folder": "Photos"}, "error": None, "kept_tier": "T1",
                       "kept": {"folder": "Photos"}, "same": True}
        with pytest.raises(ApiError) as e:
            cl.post(f"/samples/{s['id']}/replay", {"tier": "T9"})
        assert e.value.status == 422


# ---------------------------------------------------------------- P3: proof that learning works


def test_a_job_shows_the_lessons_it_used_and_the_trend_shows_before_and_after(tmp_path):
    replies = {"small": [{"folder": "Invoices"}, {"folder": "Photos"},
                         {"folder": "Other"},     # tests before the lessons: fail
                         {"folder": "Photos"},    # with them: pass
                         {"folder": "Bills"},     # job 3, with the lessons
                         {"folder": "Photos"},    # compare: with the lessons
                         {"folder": "Other"}],    # compare: without them
               "big": [{"folder": "Bills"}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        j1 = run_job(cl, w, name="invoice-oct.pdf")
        assert "lessons" not in cl.get(f"/jobs/{j1['id']}/samples")[0]  # none yet
        s2 = cl.get(f"/jobs/{run_job(cl, w, name='beach.jpg')['id']}/samples")[0]
        cl.post(f"/samples/{s2['id']}/verdict", {"verdict": "correct"})
        r = cl.post("/guidance/review")
        review = work(cl, w, r["job_id"], until=("waiting", "dead"))
        assert review["state"] == "waiting", review["error"]
        a = cl.get("/approvals?state=pending")[0]
        cl.post(f"/approvals/{a['id']}/decide", {"answer": "approve"})
        work(cl, w, review["id"])

        j3 = run_job(cl, w, name="bill-nov.pdf")
        used = cl.get(f"/jobs/{j3['id']}/samples")[0]["lessons"]
        assert used["count"] == 2 and used["approved_at"] and "invoice or a bill" in used["text"]

        key = s2["playbook"]
        t = cl.get(f"/guidance/{key}/trend?weeks=2")
        assert len(t["weeks"]) == 2 and t["weeks"][0]["answers"] == 0
        now = t["weeks"][-1]
        assert (now["answers"], now["escalated"], now["first_right"], now["with_lessons"]) == (3, 1, 2, 1)
        assert now["rate"] == round(2 / 3, 3)
        assert [x["state"] for x in t["lessons"]] == ["active"]

        q = cl.post(f"/guidance/{key}/evals?compare=true")
        res = work(cl, w, q["job_id"])["result"]
        assert res["passed"] == 1 and res["without_lessons"]["passed"] == 0
        runs = cl.get(f"/guidance/{key}/evals")["runs"]
        assert [x["why"] for x in runs][-2:] == ["manual", "without lessons"]
        assert cl.get("/guidance?plugin=sorter")[0]["last_run"]["passed"] == 1  # the run with the lessons


def test_a_second_candidate_fixes_what_the_first_got_wrong(tmp_path):
    replies = {"small": [{"folder": "Invoices"}, {"folder": "Photos"},
                         {"folder": "Photos"},    # tests now: pass
                         {"folder": "Other"},     # with the first lessons: fail
                         {"folder": "Photos"}],   # with the improved ones: pass
               "big": [{"folder": "Bills"}]}
    with FakeOllama(replies) as ol, Server(make(tmp_path).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        run_job(cl, w, name="invoice-oct.pdf")
        s2 = cl.get(f"/jobs/{run_job(cl, w, name='beach.jpg')['id']}/samples")[0]
        cl.post(f"/samples/{s2['id']}/verdict", {"verdict": "correct"})
        r = cl.post("/guidance/review")
        review = work(cl, w, r["job_id"], until=("succeeded", "waiting", "dead"))
        assert review["state"] == "waiting", review["error"]
        asked = (tmp_path / "claude_stdin.txt").read_text(encoding="utf-8")
        assert "still_wrong" in asked and "Photos" in asked  # Claude saw the test it failed
        a = cl.get("/approvals?state=pending")[0]
        assert "Tests: 1/1 now, 1/1 with these." in a["payload"]["summary"]


def test_picking_the_better_candidate():
    from argus.worker.review import better, lines_of

    assert lines_of("Sure:\n- one\n* two\nnot a rule\n-three") == "- one\n- two\n- three"
    a = {"passed": 2}
    assert better(None, a, "", "- x") and not better(a, None, "- x", "")
    assert better(a, {"passed": 3}, "- x", "- x\n- y")
    assert better(a, {"passed": 2}, "- x\n- y", "- x")  # a tie: the shorter one
    assert not better(a, {"passed": 2}, "- x", "- x\n- y")
