"""The guidance loop: answers kept, your verdicts, the nightly review's lessons with before/after scores, your
approval, and the lessons in the playbook from then on."""

from __future__ import annotations

import json
from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
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
