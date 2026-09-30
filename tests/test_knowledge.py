"""knowledge: indexing your files (contents where allowed, names elsewhere), search by words and meaning,
find by name, only what changed is read again."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from fakes import FakeOllama
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]


def setup(tmp_path: Path, monkeypatch) -> tuple[Argus, Path]:
    home = tmp_path / "home"
    for d in ("Documents/notes", "Documents/node_modules/x", "Downloads", "Pictures"):
        (home / d).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / "Documents/notes/laptop-server.md").write_text(
        "# Laptop server\n\nThe spare laptop runs Ubuntu Server 24.04 headless. It hosts Argus, Cashly and the "
        "backups. BIOS: power on after AC loss.\n")
    (home / "Documents/recipe.txt").write_text("Chicken curry: onions, garlic, coconut milk, 40 minutes.")
    with zipfile.ZipFile(home / "Documents/cv.docx", "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Sasanka - software engineer at a "
                                        "fintech company</w:t></w:r></w:p></w:body></w:document>")
    (home / "Documents/node_modules/x/readme.md").write_text("laptop server laptop server")  # skipped folder
    (home / "Downloads/invoice-october.pdf").write_bytes(b"%PDF-1.4 not really")
    (home / "Pictures/beach.jpg").write_bytes(b"\xff\xd8 not really")
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "models:\n  tiers:\n    T1: {provider: ollama, model: 'qwen2.5-coder:7b'}\n  chain: [T1]\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: [knowledge]\n"
        "  config:\n    knowledge: {folders: ['~/Documents'], names_only: ['~/Downloads', '~/Pictures']}\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml")), home


def run(cl, w, workflow, **inp):
    job = cl.post("/jobs", {"plugin": "knowledge", "workflow": workflow, "needs": ["desktop"], "input": inp})
    assert w.run_once(wait=2)
    j = wait_for(lambda: (x := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and x)
    assert j["state"] == "succeeded", j["error"]
    return j["result"]


def test_index_search_find(tmp_path, monkeypatch):
    argus, home = setup(tmp_path, monkeypatch)
    with FakeOllama({"qwen2.5-coder:7b": ["{}"], "nomic-embed-text": ["x"]}) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        assert "knowledge" in w.plugins, w.plugin_errors
        r = run(cl, w, "index")
        assert r["files"] == 5 and r["with_contents"] == 3 and r["with_meaning"] >= 3, r
        hits = run(cl, w, "search", query="what does my spare laptop run?")["results"]
        assert hits and hits[0]["file"].endswith("laptop-server.md") and "Ubuntu" in hits[0]["passage"]
        assert all("node_modules" not in h["file"] for h in hits)
        cv = run(cl, w, "search", query="my job as a software engineer")["results"]
        assert any(h["file"].endswith("cv.docx") for h in cv)
        found = run(cl, w, "find", name="invoice")["files"]
        assert [os.path.basename(f["path"]) for f in found] == ["invoice-october.pdf"]
        assert run(cl, w, "search", query="beach")["results"] == []  # pictures: by name only
        # only what changed is read again
        (home / "Documents/recipe.txt").write_text("Fish curry: tuna, goraka, 25 minutes.")
        os.utime(home / "Documents/recipe.txt", (1, 2))
        (home / "Documents/cv.docx").unlink()
        again = run(cl, w, "index")
        assert again["changed"] == 1 and again["removed"] == 1 and again["files"] == 4
        fish = run(cl, w, "search", query="goraka fish")["results"]
        assert fish and "tuna" in fish[0]["passage"]
        assert all("Chicken" not in h["passage"] for h in run(cl, w, "search", query="chicken coconut")["results"])


def test_words_only_without_the_embedding_model(tmp_path, monkeypatch):
    argus, home = setup(tmp_path, monkeypatch)
    with FakeOllama({"qwen2.5-coder:7b": ["{}"]}) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = run(cl, w, "index")
        assert r["with_meaning"] == 0 and r["with_contents"] == 3
        hits = run(cl, w, "search", query="laptop ubuntu")["results"]
        assert hits and hits[0]["file"].endswith("laptop-server.md")
