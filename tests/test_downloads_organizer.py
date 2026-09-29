"""The downloads-organizer plugin: grouping rules, and a full run through argusd, a worker and (fake) Ollama."""

from __future__ import annotations

import importlib.util
import json
import os
import time
import urllib.request
from pathlib import Path

import pytest

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.client import ApiError
from fakes import FakeOllama
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "downloads-organizer"
spec = importlib.util.spec_from_file_location("dorg_under_test", PLUGIN_DIR / "plugin.py")
dorg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dorg)


def test_names_reduce_to_what_they_are_about():
    for name in ("resume_v2.pdf", "resume (1).pdf", "resume-final.pdf", "Resume 2026-09-01.pdf"):
        assert dorg.norm_stem(name) == "resume", name
    assert dorg.norm_stem("structural_break_v1.ipynb") == dorg.norm_stem("structural_break_v2.ipynb")
    assert dorg.group_label(["lecture_07.pdf", "lecture_08.pdf"]) == "lecture"
    assert dorg.group_label(["deck_v1.pptx", "deck_v2.pptx"], "deck") == "deck"
    assert dorg.safe_folder('a<b>:"c') == "abc"


def test_groups_and_existing_folders():
    files = [{"name": n} for n in ("lecture_07.pdf", "lecture_08.pdf", "a.txt", "b.txt", "notes.md")]
    groups = {g["label"] or g["members"][0]["name"]: len(g["members"]) for g in dorg.build_groups(files)}
    assert groups == {"lecture": 2, "a.txt": 1, "b.txt": 1, "notes.md": 1}  # short names never group
    idx = {"deeplense": ("Projects", "/d/Projects/DeepLense"), "deep": ("Misc", "/d/Misc/deep")}
    assert dorg.match_existing(idx, "deeplense")[1].endswith("DeepLense")  # the longest match wins
    assert dorg.match_existing(idx, "deeplense notes")[0] == "Projects"
    assert dorg.match_existing(idx, "deeplensex") is None
    assert dorg.normalise("pictures") == "Images" and dorg.normalise("Lectures") is None


OLD = time.time() - 3600


def downloads(home: Path) -> Path:
    d = home / "Downloads"
    (d / "Projects" / "DeepLense").mkdir(parents=True)
    for n in ("lecture_07.pdf", "lecture_08.pdf", "train_model.py", "DeepLense_v2.zip", "weird.bin",
              "big.iso.crdownload", "desktop.ini"):
        (d / n).write_text(n)
        os.utime(d / n, (OLD, OLD))
    (d / "fresh.pdf").write_text("still downloading?")  # younger than min_age: left for later
    return d


def setup(tmp_path: Path, monkeypatch, live: bool) -> tuple[Argus, Path]:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))  # the manifest says ~/Downloads
    monkeypatch.setenv("USERPROFILE", str(home))
    d = downloads(home)
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: {[dorg.PLUGIN] if live else []}\n"
        f"  config: {{{dorg.PLUGIN}: {{min_age_seconds: 60}}}}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml")), d


REPLIES = {"qwen2.5-coder:7b": [{"category": "Documents", "reason": "lecture notes"},
                                {"category": "Projects", "reason": "python script"},
                                {"category": "Stuff", "reason": "?"}],  # not a category: T1 fails, T2 answers
           "qwen2.5-coder:14b": [{"category": "Misc", "reason": "unknown binary"}]}


def run_job(argus: Argus, ol: FakeOllama, submit) -> dict:
    with Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        assert dorg.PLUGIN in w.plugins, w.plugin_errors
        job = submit(cl)
        assert w.run_once(wait=2)
        return wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead", "retry")
                        and j)


def names(d: Path) -> set[str]:
    return {str(p.relative_to(d)).replace(os.sep, "/") for p in d.rglob("*") if p.is_file()}


def test_sweep_live_sorts_groups_and_escalates(tmp_path, monkeypatch):
    argus, d = setup(tmp_path, monkeypatch, live=True)
    with FakeOllama(REPLIES) as ol:
        job = run_job(argus, ol, lambda cl: cl.post(f"/plugins/{dorg.PLUGIN}/run", {}))
    assert job["state"] == "succeeded", job.get("error")
    assert names(d) == {"Documents/lecture/lecture_07.pdf", "Documents/lecture/lecture_08.pdf",
                        "Projects/train_model.py", "Projects/DeepLense/DeepLense_v2.zip", "Misc/weird.bin",
                        "fresh.pdf", "big.iso.crdownload", "desktop.ini"}
    moved = {m["name"]: m for m in job["result"]["moved"]}
    assert set(moved) == {"lecture_07.pdf", "lecture_08.pdf", "train_model.py", "DeepLense_v2.zip", "weird.bin"}
    assert len(ol.requests) == 5  # one per group, the existing folder needs none, plus T1's two tries and T2


def test_dry_run_moves_nothing_and_file_job_takes_its_siblings(tmp_path, monkeypatch):
    argus, d = setup(tmp_path, monkeypatch, live=False)
    before = names(d)
    with FakeOllama(REPLIES) as ol:
        job = run_job(argus, ol, lambda cl: cl.post("/jobs", {"plugin": dorg.PLUGIN, "workflow": "file",
                                                               "input": {"path": str(d / "lecture_07.pdf")},
                                                               "needs": ["desktop"]}))
    assert job["state"] == "succeeded", job.get("error")
    res = job["result"]
    assert res["dry_run"] is True and names(d) == before
    assert "moved" not in res and res["mode"].startswith("dry-run")
    assert sorted(Path(m["to"]).name for m in res["would_move"]) == ["lecture_07.pdf", "lecture_08.pdf"]
    assert all(Path(m["to"]).parent.name == "lecture" for m in res["would_move"])


def test_file_job_for_a_file_already_gone(tmp_path, monkeypatch):
    argus, d = setup(tmp_path, monkeypatch, live=True)
    with FakeOllama(REPLIES) as ol:
        job = run_job(argus, ol, lambda cl: cl.post("/jobs", {"plugin": dorg.PLUGIN, "workflow": "file",
                                                               "input": {"path": str(d / "nope.pdf")},
                                                               "needs": ["desktop"]}))
    assert job["state"] == "succeeded" and job["result"]["note"] == "already gone" and not ol.requests


@pytest.mark.parametrize("bad", ["../outside.txt"])
def test_undo_stays_inside_downloads(tmp_path, monkeypatch, bad):
    argus, d = setup(tmp_path, monkeypatch, live=True)
    with FakeOllama(REPLIES) as ol:
        job = run_job(argus, ol, lambda cl: cl.post(f"/plugins/{dorg.PLUGIN}/run", {
            "workflow": "undo", "input": {"from": str(d / "weird.bin"), "to": str(d / ".." / bad)}}))
    assert job["state"] == "dead" and "may not write" in job["error"]
    assert (d / "weird.bin").exists()


def test_undo_a_move_from_the_job_changes(tmp_path, monkeypatch):
    argus, d = setup(tmp_path, monkeypatch, live=True)
    with FakeOllama(REPLIES) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        job = cl.post(f"/plugins/{dorg.PLUGIN}/run", {})
        assert w.run_once(wait=2)
        wait_for(lambda: cl.get(f"/jobs/{job['id']}")["state"] == "succeeded")
        changes = cl.get(f"/jobs/{job['id']}/changes")
        assert len(changes) == 5 and all(c["can_undo"] for c in changes)
        c = next(c for c in changes if c["to"].endswith("weird.bin"))
        u = cl.post(f"/jobs/{job['id']}/changes/{c['event_id']}/undo")
        assert u["created"] and cl.post(f"/jobs/{job['id']}/changes/{c['event_id']}/undo")["id"] == u["id"]
        assert w.run_once(wait=2)
        wait_for(lambda: cl.get(f"/jobs/{u['id']}")["state"] == "succeeded")
        after = {x["event_id"]: x for x in cl.get(f"/jobs/{job['id']}/changes")}
        assert after[c["event_id"]]["undo"]["state"] == "succeeded" and not after[c["event_id"]]["can_undo"]
        assert cl.post(f"/jobs/{job['id']}/changes/{c['event_id']}/undo") == {"id": u["id"], "created": False}
        assert [j["workflow"] for j in cl.get(f"/jobs?plugin={dorg.PLUGIN}")] == ["undo", "sort"]
    assert (d / "weird.bin").exists() and not (d / "Misc" / "weird.bin").exists()


def test_wrong_button_fixes_the_file_and_teaches_the_models(tmp_path, monkeypatch):
    argus, d = setup(tmp_path, monkeypatch, live=True)
    with FakeOllama(REPLIES) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        job = cl.post(f"/plugins/{dorg.PLUGIN}/run", {})
        assert w.run_once(wait=2)
        wait_for(lambda: cl.get(f"/jobs/{job['id']}")["state"] == "succeeded")
        assert cl.get(f"/plugins/{dorg.PLUGIN}/state/choices")["value"] == list(dorg.CATEGORIES)
        info = next(p for p in cl.get("/plugins")["plugins"] if p["id"] == dorg.PLUGIN)
        assert info["wrong"] == {"workflow": "correct", "label": "Wrong folder"}
        changes = cl.get(f"/jobs/{job['id']}/changes")
        lec = next(c for c in changes if c["to"].endswith("lecture_07.pdf"))
        fix = cl.post(f"/jobs/{job['id']}/changes/{lec['event_id']}/wrong", {"value": "Private"})
        assert w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{fix['id']}"))["state"] in ("succeeded", "dead") and j)
        assert done["state"] == "succeeded", done["error"]
        after = next(c for c in cl.get(f"/jobs/{job['id']}/changes") if c["event_id"] == lec["event_id"])
        assert after["fix"]["value"] == "Private" and not after["can_fix"] and not after["can_undo"]
        assert cl.get(f"/plugins/{dorg.PLUGIN}/state/learned")["value"] == {"lecture_07.pdf": "Private"}
        # the next decision sees the correction as an example
        (d / "slides_week3.pdf").write_text("x")
        os.utime(d / "slides_week3.pdf", (OLD, OLD))
        cl.post(f"/plugins/{dorg.PLUGIN}/run", {})
        assert w.run_once(wait=2)
        system = ol.requests[-1]["messages"][0]["content"]
        assert '"lecture_07.pdf" -> Private' in system
    assert (d / "Private" / "lecture" / "lecture_07.pdf").exists()  # keeps its group folder


def test_share_from_the_phone_lands_sorted(tmp_path, monkeypatch):
    argus, d = setup(tmp_path, monkeypatch, live=True)
    replies = {"qwen2.5-coder:7b": [{"category": "Web", "reason": "a link"},  # groups go by name: "Argus repo" first
                                    {"category": "Private", "reason": "bank statement"}]}
    with FakeOllama(replies) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        targets = cl.get("/share/targets")
        assert {"plugin": dorg.PLUGIN, "workflow": "take"}.items() <= targets[0].items()
        share = cl.post("/shares", {"title": "Argus repo", "url": "https://github.com/SasankaRW/argus"})
        req = urllib.request.Request(f"{srv.url}/shares/{share['id']}/files?name=bank-statement-sep.pdf"
                                     "&type=application/pdf", data=b"%PDF-1.4 fake", method="PUT")
        meta = json.loads(urllib.request.urlopen(req).read())
        assert meta["files"] == [{"name": "bank-statement-sep.pdf", "type": "application/pdf", "size": 13}]
        sent = cl.post(f"/shares/{share['id']}/send", {"plugin": dorg.PLUGIN, "workflow": "take"})
        assert cl.post(f"/shares/{share['id']}/send", {"plugin": dorg.PLUGIN, "workflow": "take"})["id"] == sent["id"]
        assert w.run_once(wait=2)
        job = wait_for(lambda: (j := cl.get(f"/jobs/{sent['id']}"))["state"] in ("succeeded", "dead") and j)
        assert job["state"] == "succeeded", job["error"]
    assert (d / "Private" / "bank-statement-sep.pdf").read_bytes() == b"%PDF-1.4 fake"
    url = d / "Web" / "Argus repo.url"
    assert "URL=https://github.com/SasankaRW/argus" in url.read_text()


def test_share_rules(tmp_path, monkeypatch):
    argus, d = setup(tmp_path, monkeypatch, live=True)
    with Server(argus.open()) as srv:
        cl = client(srv.url)
        share = cl.post("/shares", {})
        with pytest.raises(ApiError) as e:  # empty
            cl.post(f"/shares/{share['id']}/send", {"plugin": dorg.PLUGIN, "workflow": "take"})
        assert e.value.status == 422
        with pytest.raises(ApiError) as e:
            cl.post(f"/shares/{share['id']}/send", {"plugin": dorg.PLUGIN, "workflow": "sort"})  # not a share target
        assert e.value.status == 422
        with pytest.raises(ApiError) as e:
            cl.get("/shares/..%2F..%2Fetc/files/passwd")
        assert e.value.status == 404


def test_rules_edited_in_helios_apply_to_the_next_job(tmp_path, monkeypatch):
    argus, d = setup(tmp_path, monkeypatch, live=True)
    (d / "Pictures").mkdir()
    (d / "Pictures" / "cat.jpg").write_text("meow")
    (d / "Images").mkdir()
    (d / "Images" / "cat.jpg").write_text("another cat")  # a clash: the moved one becomes "cat (1).jpg"
    replies = {"qwen2.5-coder:7b": [{"category": "Books", "reason": "lecture notes"}]}
    with FakeOllama(replies) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = cl.get(f"/plugins/{dorg.PLUGIN}/rules")
        assert r["custom"] is False and "categories:" in r["text"] and r["label"] == "Sorting rules"
        # broken YAML never gets saved
        with pytest.raises(ApiError) as e:
            cl.call("PUT", f"/plugins/{dorg.PLUGIN}/rules", {"text": "categories: [oops"})
        assert e.value.status == 422 and "line" in str(e.value.body)

        def run(workflow: str, **inp) -> dict:
            job = cl.post(f"/plugins/{dorg.PLUGIN}/run", {"workflow": workflow, "input": inp})
            assert w.run_once(wait=2)
            return wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)

        # a category that doesn't exist in rules.yaml: the model's "Books" is accepted once the rules have it
        cl.call("PUT", f"/plugins/{dorg.PLUGIN}/rules", {"text": r["text"].replace(
            "categories:\n", "categories:\n  Books: lecture notes, ebooks, papers\n")})
        job = run("sort")
        assert job["state"] == "succeeded", job["error"]
        assert "Books" in ol.requests[0]["messages"][0]["content"]
        assert (d / "Books" / "lecture" / "lecture_07.pdf").exists()
        # rules that make no sense stop the job with a message that says what to fix
        cl.call("PUT", f"/plugins/{dorg.PLUGIN}/rules", {"text": "categories:\n  Docs: x\nmerge: {Pictures: Nope}\n"})
        bad = run("sort")
        assert bad["state"] == "dead" and "merge: Pictures -> Nope" in bad["error"]
        # back to the defaults, then tidy: Pictures folds into Images
        cl.call("DELETE", f"/plugins/{dorg.PLUGIN}/rules")
        assert cl.get(f"/plugins/{dorg.PLUGIN}/rules")["custom"] is False
        tidy = run("tidy")
        assert tidy["state"] == "succeeded", tidy["error"]
        assert tidy["result"]["merged"] == [{"folder": "Pictures", "into": "Images", "moved": 1, "removed": True}]
    assert not (d / "Pictures").exists()
    assert sorted(p.name for p in (d / "Images").iterdir()) == ["cat (1).jpg", "cat.jpg"]
