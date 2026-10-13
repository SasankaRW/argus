"""Notes vault: filing Inbox notes under the right topic, links both ways, enhancing marked notes, Life Hub notes,
and the file sandbox's replace_text (only in `edit` folders, old version kept, never over a newer copy)."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.plugins import FileChanged, Files, PermissionDenied
from fakes import FakeOllama, fake_claude
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t_vault", ROOT / "plugins" / "vault" / "plugin.py")
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)

MODEL = "qwen2.5-coder:7b"


# ------------------------------------------------------------------ pieces

def test_frontmatter_round_trip_and_sections():
    meta, body = vt.split_fm("---\narea: Docker\ntags: [docker, images]\ncreated: 2026-10-13\nenhance: false\n---\n"
                             "# Layers\n\ntext\n")
    assert meta == {"area": "Docker", "tags": ["docker", "images"], "created": "2026-10-13", "enhance": False}
    assert body == "# Layers\n\ntext\n"
    again, _ = vt.split_fm(vt.dump_fm({**meta, "sources": ["https://x.y/a?b=1"], "title": "a: b"}) + "x")
    assert again["sources"] == ["https://x.y/a?b=1"] and again["title"] == "a: b" and again["enhance"] is False
    assert vt.split_fm("no frontmatter") == ({}, "no frontmatter")
    assert vt.split_fm("---\n: [broken\n---\nx") == ({}, "---\n: [broken\n---\nx")

    body = "# T\n\nmine\n"
    body = vt.add_section(body, "2026-10-13", "new bit")
    body = vt.add_related(body, ["A", "B"])
    body = vt.add_related(body, ["b", "C"])  # already linked (any case) stays once
    assert body == "# T\n\nmine\n\n## 2026-10-13\n\nnew bit\n\n## Related\n\n- [[A]]\n- [[B]]\n- [[C]]\n"
    body = vt.add_section(body, "later", "more")  # sections go before Related
    assert body.index("## later") < body.index("## Related")
    assert vt.clean_title('What is "a/b": #1?') == "What is a b 1"


def test_a_notes_parts():
    body = ("# Docker\n\n<!-- enhanced:start -->\nold enhanced\n<!-- enhanced:end -->\n\n"
            "> [!quote]- My original notes\n> first line\n>\n> second\n\nadded later #enhance\n\n"
            "## Related\n\n- [[K8s]]\n")
    p = vt.parts_of(body)
    assert p["title"] == "Docker" and p["enhanced"] == "old enhanced"
    assert p["originals"] == "first line\n\nsecond" and p["mine"] == "added later"
    assert p["related"] == "## Related\n\n- [[K8s]]"
    assert vt.marked({}, "x #enhance") and vt.marked({"enhance": True}, "x") and not vt.marked({}, "#enhanced")


def test_replace_text_only_in_edit_folders_keeps_the_old_version(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    v = tmp_path / "v"
    v.mkdir()
    note = v / "n.md"
    note.write_text("old", encoding="utf-8")
    events = []
    f = Files("p", {"read": [str(v)], "write": [str(v)]}, {}, False, lambda k, d: events.append((k, d)))
    with pytest.raises(PermissionDenied, match="permissions.files.edit"):
        f.replace_text(note, "new")
    f = Files("p", {"read": [str(v)], "write": [str(v)], "edit": [str(v)]}, {}, False,
              lambda k, d: events.append((k, d)))
    with pytest.raises(FileChanged):
        f.replace_text(note, "new", expect_mtime=note.stat().st_mtime - 50)
    backup = f.replace_text(note, "new", expect_mtime=note.stat().st_mtime)
    assert note.read_text(encoding="utf-8") == "new" and Path(backup).read_text(encoding="utf-8") == "old"
    assert ".argus-trash" in backup and events[-1][0] == "file.updated"
    dry = Files("p", {"read": [str(v)], "write": [str(v)], "edit": [str(v)]}, {}, True, lambda k, d: None)
    dry.replace_text(note, "newer")
    assert note.read_text(encoding="utf-8") == "new"


# ------------------------------------------------------------------ a real Argus

def setup(tmp_path, monkeypatch, extra="", config="filing: auto"):
    home = tmp_path / "home"
    (home / "Vault" / "00 Inbox").mkdir(parents=True)
    (home / "Vault" / "Home.md").write_text("# Home\n\n## Areas\n\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        "ollama:\n  url: http://127.0.0.1:9\n"
        f"models:\n  tiers:\n    T1: {{provider: ollama, model: '{MODEL}'}}\n" + (extra or "  chain: [T1]\n") +
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: [vault]\n"
        f"  config:\n    vault: {{{config}}}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml")), home / "Vault"


def run(cl, w, workflow, **inp):
    job = cl.post("/jobs", {"plugin": "vault", "workflow": workflow, "input": inp})
    w.run_once(wait=1)
    return wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead", "waiting") and j)


def age(path: Path, seconds: float = 600) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_capture_then_file_a_new_topic_then_add_to_it(tmp_path, monkeypatch):
    replies = {MODEL: [
        {"action": "new", "area": "Docker", "title": "Image Layers", "tags": ["docker"],
         "summary": "How image layers are cached", "confidence": 0.9},
        {"action": "add", "note": "Image Layers", "confidence": 0.9, "summary": "x"},
        {"action": "new", "area": "Docker", "title": "Compose Networks", "tags": ["docker", "net"],
         "summary": "Service names resolve on the default network", "confidence": 0.9},
    ], "nomic-embed-text": [{}]}  # search by meaning
    argus, vault = setup(tmp_path, monkeypatch)
    with FakeOllama(replies) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        assert "vault" in w.plugins, w.plugin_errors
        assert {"save_to_vault", "enhance_vault_note", "find_in_vault"} <= {t["name"] for t in cl.get("/tools")}

        r = run(cl, w, "capture", text="Each RUN makes a layer; order steps so the slow ones are cached.",
                topic="Docker")
        assert r["state"] == "succeeded", r["error"]
        cap = Path(r["result"]["saved"])
        assert cap.parent == vault / "00 Inbox"
        meta, body = vt.split_fm(cap.read_text(encoding="utf-8"))
        assert meta == {"source": "ari", "topic": "Docker"} and body.startswith("Each RUN makes a layer")

        r = run(cl, w, "file", path=str(cap))
        assert r["state"] == "succeeded", r["error"]
        note = vault / "Topics" / "Docker" / "Image Layers.md"
        meta, body = vt.split_fm(note.read_text(encoding="utf-8"))
        assert meta["area"] == "Docker" and meta["tags"] == ["docker"] and meta["status"] == "seed"
        assert body == "# Image Layers\n\nEach RUN makes a layer; order steps so the slow ones are cached.\n"
        assert (vault / "00 Inbox" / "_done" / cap.name).exists() and not cap.exists()
        assert (vault / "Maps" / "Docker map.md").read_text(encoding="utf-8").endswith(
            "- [[Image Layers]] – How image layers are cached\n")
        assert "- [[Docker map|Docker]]" in (vault / "Home.md").read_text(encoding="utf-8")

        # the same subject again: a dated section in that note, the words as written
        age(note)
        cap2 = vault / "00 Inbox" / "more.md"
        cap2.write_text("Use .dockerignore so the context stays small.", encoding="utf-8")
        r = run(cl, w, "file", path=str(cap2))
        assert r["state"] == "succeeded" and r["result"]["action"] == "add", r
        text = note.read_text(encoding="utf-8")
        assert text.count("Each RUN makes a layer") == 1
        assert "\n\nUse .dockerignore so the context stays small.\n" in text
        assert vt.split_fm(text)[0]["status"] == "growing"
        assert list((Path.home() / ".argus-trash" / "versions").rglob("*Image Layers.md"))  # the old version

        # a related new topic: linked both ways
        age(note)
        cap3 = vault / "00 Inbox" / "net.md"
        cap3.write_text("Docker compose: containers reach each other by service name on the default network. "
                        "Image layers are shared too.", encoding="utf-8")
        r = run(cl, w, "file", path=str(cap3))
        assert r["state"] == "succeeded", r["error"]
        new = (vault / "Topics" / "Docker" / "Compose Networks.md").read_text(encoding="utf-8")
        assert "## Related\n\n- [[Image Layers]]\n" in new
        assert "- [[Compose Networks]]" in note.read_text(encoding="utf-8")
        # a note outside the Inbox is not filed
        r = run(cl, w, "file", path=str(vault / "Home.md"))
        assert r["result"]["why"] == "not a note in the Inbox"

        found = run(cl, w, "find", query="dockerignore context")["result"]["notes"]
        assert found and found[0]["title"] == "Image Layers" and "dockerignore" in found[0]["passage"]


def test_filing_asks_where_it_goes_and_your_answer_wins(tmp_path, monkeypatch):
    replies = {MODEL: [{"action": "new", "area": "Misc", "title": "Thing", "confidence": 0.9}]}
    argus, vault = setup(tmp_path, monkeypatch, config="filing: ask")
    with FakeOllama(replies) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        cap = vault / "00 Inbox" / "q.md"
        cap.write_text("Attention weights are a softmax over query-key scores.", encoding="utf-8")
        r = run(cl, w, "file", path=str(cap))
        assert r["state"] == "waiting"
        a = wait_for(lambda: cl.get("/approvals?state=pending"))[0]
        assert set(a) >= {"id", "title"} and "Where should" in a["title"], a
        assert json.dumps({"area": "Misc", "title": "Thing", "add_to": ""})[1:-1] in json.dumps(a), a
        cl.post(f"/approvals/{a['id']}/decide",
                {"answer": "approve", "fields": {"area": "Machine Learning", "title": "Attention", "add_to": ""}})
        w.run_once(wait=2)
        wait_for(lambda: cl.get(f"/jobs/{r['id']}")["state"] == "succeeded")
        assert (vault / "Topics" / "Machine Learning" / "Attention.md").exists()

        # a "no" leaves it in the Inbox, and tidy doesn't ask again
        cap2 = vault / "00 Inbox" / "r.md"
        cap2.write_text("something odd", encoding="utf-8")
        r = run(cl, w, "file", path=str(cap2))
        a = wait_for(lambda: cl.get("/approvals?state=pending"))[0]
        cl.post(f"/approvals/{a['id']}/decide", {"answer": "reject"})
        w.run_once(wait=2)
        done = wait_for(lambda: (j := cl.get(f"/jobs/{r['id']}"))["state"] == "succeeded" and j)
        assert "stays in the Inbox" in done["result"]["why"] and cap2.exists()
        age(cap2)
        t = run(cl, w, "tidy")
        assert t["state"] == "succeeded" and t["result"]["filed"] == [], t


def test_enhancing_a_marked_note_keeps_your_words(tmp_path, monkeypatch):
    out = ("Attention lets each token weigh the others.\n\n## Key ideas\n\n- Scores are softmaxed. See [[Transformers]]"
           " and [[Made Up Note]].\n\n## Sources\n\n- [Paper](https://arxiv.org/abs/1706.03762)\n" + "x " * 100)
    cmd = fake_claude(tmp_path, result=out)
    extra = "    T3: {provider: claude}\n  chain: [T1]\n" + f"claude:\n  command: {json.dumps(cmd)}\n"
    argus, vault = setup(tmp_path, monkeypatch, extra=extra)
    ml = vault / "Topics" / "Machine Learning"
    ml.mkdir(parents=True)
    note = ml / "Attention.md"
    note.write_text("---\narea: Machine Learning\ncreated: 2026-10-01\nstatus: seed\n---\n\n# Attention\n\n"
                    "softmax(QK^T) V, my words #enhance\n\n## Related\n\n- [[Transformers]]\n", encoding="utf-8")
    (ml / "Transformers.md").write_text("# Transformers\n\nstack of attention\n", encoding="utf-8")
    age(note)
    age(ml / "Transformers.md")
    with FakeOllama({MODEL: [{}]}) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = run(cl, w, "tidy")
        assert r["state"] == "succeeded", r["error"]
        assert [e["enhanced"] for e in r["result"]["enhanced"]] == ["Topics/Machine Learning/Attention.md"]
    meta, body = vt.split_fm(note.read_text(encoding="utf-8"))
    assert meta["enhance"] is False and meta["enhanced"] and meta["status"] == "growing"
    assert meta["sources"] == ["https://arxiv.org/abs/1706.03762"]
    assert "<!-- enhanced:start -->\nAttention lets each token" in body
    assert "[[Transformers]]" in body and "[[Made Up Note]]" not in body and "Made Up Note" in body
    assert "> [!quote]- My original notes\n> softmax(QK^T) V, my words\n" in body and "#enhance" not in body
    assert body.rstrip().endswith("## Related\n\n- [[Transformers]]")
    sent = (tmp_path / "claude_stdin.txt").read_text(encoding="utf-8")
    assert "softmax(QK^T) V, my words" in sent and "stack of attention" not in sent  # only the marked note
    assert "WebSearch,WebFetch" in json.loads((tmp_path / "claude_argv.json").read_text())


def serve_lifehub(rows):
    calls = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(("GET", self.path, self.headers.get("X-API-Key")))
            data = json.dumps(rows).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_DELETE(self):
            calls.append(("DELETE", self.path, self.headers.get("X-API-Key")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", calls


def test_tidy_takes_life_hub_notes_and_tells_about_conflicts(tmp_path, monkeypatch):
    base, calls = serve_lifehub([{"id": 7, "uid": "abcdefgh-1", "name": "Rust", "notes": "Rust: borrow checker rules",
                                  "priority": 1, "deleted": False}])
    (tmp_path / ".env").write_text("LIFEHUB_API_KEY=k3y\n", encoding="utf-8")
    argus, vault = setup(tmp_path, monkeypatch, config=f"filing: auto, lifehub_url: '{base}'")
    (vault / "Topics" / "Rust").mkdir(parents=True)
    (vault / "Topics" / "Rust" / "Ownership.sync-conflict-20261013-101010-ABCDEF.md").write_text("x", encoding="utf-8")
    with FakeOllama({MODEL: [{}]}) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = run(cl, w, "tidy")
        assert r["state"] == "succeeded", r["error"]
        assert r["result"]["from_lifehub"] == 1 and len(r["result"]["conflicts"]) == 1
        caps = list((vault / "00 Inbox").glob("*.md"))
        assert len(caps) == 1
        meta, body = vt.split_fm(caps[0].read_text(encoding="utf-8"))
        assert meta == {"source": "lifehub", "enhance": True} and body == "Rust: borrow checker rules\n"
        assert ("DELETE", "/api/items/7", "k3y") in calls and calls[0][1] == "/api/items?list=learn"
        r2 = run(cl, w, "tidy")
        assert r2["result"]["conflicts"] == []  # told once


# ------------------------------------------------------------------ Syncthing setup (scripts/vault_sync.py)

ME = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
LAPTOP = "LLLLLLL-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"


def fake_syncthing():
    state = {"options": {}, "folders": {}, "devices": {ME: {"deviceID": ME, "name": "pc"}}, "keys": set()}

    class H(BaseHTTPRequestHandler):
        def _send(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def do_GET(self):
            state["keys"].add(self.headers.get("X-API-Key"))
            p = self.path
            if p == "/rest/system/status":
                self._send({"myID": ME})
            elif p == "/rest/config/folders":
                self._send(list(state["folders"].values()))
            elif p == "/rest/config/devices":
                self._send(list(state["devices"].values()))
            elif p == "/rest/system/connections":
                self._send({"connections": {LAPTOP: {"connected": True}}})
            elif p.startswith("/rest/db/status"):
                self._send({"state": "idle", "localFiles": 3, "needFiles": 0})
            else:
                self.send_response(404)
                self.end_headers()

        def do_PUT(self):
            kind, key = self.path.split("/")[3:5]
            state[kind][key] = self._body()
            self._send({})

        def do_PATCH(self):
            parts = self.path.split("/")
            if parts[3] == "options":
                state["options"].update(self._body())
            else:
                state[parts[3]][parts[4]].update(self._body())
            self._send({})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"127.0.0.1:{srv.server_address[1]}", state


def test_vault_sync_sets_up_syncthing_on_the_tailnet_only(tmp_path, capsys):
    spec2 = importlib.util.spec_from_file_location("t_vault_sync", ROOT / "scripts" / "vault_sync.py")
    vs = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(vs)
    gui, st = fake_syncthing()
    cfg = tmp_path / "config.xml"
    cfg.write_text("<configuration><gui><apikey>secret123</apikey></gui></configuration>", encoding="utf-8")
    vault = tmp_path / "Vault"
    (vault / "Home.md").parent.mkdir(parents=True)
    (vault / "Home.md").write_text("mine", encoding="utf-8")

    assert vs.main(["--gui", gui, "--config", str(cfg), "setup", "--vault", str(vault)]) == 0
    assert "This machine's device ID" in capsys.readouterr().out and st["keys"] == {"secret123"}
    assert (vault / "Home.md").read_text(encoding="utf-8") == "mine"  # yours is never replaced
    assert (vault / ".stignore").exists() and (vault / "Templates" / "Mark for enhance.md").exists()
    assert (vault / "00 Inbox").is_dir() and (vault / "Topics").is_dir()
    assert st["options"]["globalAnnounceEnabled"] is False and st["options"]["relaysEnabled"] is False
    f = st["folders"]["vault"]
    assert f["path"] == str(vault) and f["type"] == "sendreceive" and f["versioning"]["type"] == "staggered"
    assert f["devices"] == [{"deviceID": ME}]

    vs.main(["--gui", gui, "--config", str(cfg), "add", "--id", LAPTOP.lower(), "--name", "laptop",
             "--address", "100.101.102.103"])
    assert st["devices"][LAPTOP]["addresses"] == ["tcp://100.101.102.103:22000", "quic://100.101.102.103:22000"]
    assert {"deviceID": LAPTOP} in st["folders"]["vault"]["devices"]
    with pytest.raises(SystemExit, match="Tailscale"):
        vs.main(["--gui", gui, "--config", str(cfg), "add", "--id", LAPTOP, "--name", "x", "--address", "8.8.8.8"])
    with pytest.raises(SystemExit, match="device ID"):
        vs.main(["--gui", gui, "--config", str(cfg), "add", "--id", "nope", "--name", "x", "--address", "100.1.2.3"])
    vs.main(["--gui", gui, "--config", str(cfg), "setup", "--vault", str(vault)])  # again: kept, not doubled
    assert st["folders"]["vault"]["devices"] == [{"deviceID": ME}, {"deviceID": LAPTOP}]
    capsys.readouterr()
    vs.main(["--gui", gui, "--config", str(cfg), "status"])
    out = json.loads(capsys.readouterr().out)
    assert out["devices"] == {"laptop": "connected"} and out["vault"]["state"] == "idle"
