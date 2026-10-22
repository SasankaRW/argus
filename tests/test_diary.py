"""Diary: what you tell Ari about your day becomes that day's entry in Life Hub (written only from what you said,
your words kept), things you learned go on to the notes vault, and Life Hub's diary box is taken in."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from argus.config import load_config
from argus.context import Argus
from argus.worker import PermanentError, Worker
from fakes import FakeOllama, fake_claude
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t_diary", ROOT / "plugins" / "diary" / "plugin.py")
dy = importlib.util.module_from_spec(spec)
sys.modules["t_diary"] = dy  # so pydantic can see the module's names
spec.loader.exec_module(dy)

MODEL = "qwen2.5-coder:7b"
SAID = ("my day was good, I fixed the login bug and went to the gym with Kasun. "
        "I learned that Postgres VACUUM frees space")


def test_which_day():
    now = dt.datetime(2026, 10, 21, 21, 30)  # a Wednesday evening
    assert dy.which_day("today", now) == dt.date(2026, 10, 21)
    assert dy.which_day("yesterday", now) == dt.date(2026, 10, 20)
    assert dy.which_day("sunday", now) == dt.date(2026, 10, 18) and dy.which_day("last wednesday", now) == \
        dt.date(2026, 10, 14)
    assert dy.which_day("today", dt.datetime(2026, 10, 22, 1, 15)) == dt.date(2026, 10, 21)  # after midnight
    assert dy.which_day("2026-10-01", now) == dt.date(2026, 10, 1)
    with pytest.raises(PermanentError):
        dy.which_day("2026-12-01", now)


def test_the_entry_may_only_use_what_was_said():
    check = dy.entry_check("10:00  went to the gym with Kasun at 6")
    ok = dy.Entry(title="Gym day", entry="I went to the gym with Kasun at 6.", people=["Kasun"])
    assert check(ok, None) is None
    assert "numbers" in check(ok.model_copy(update={"entry": "I ran 5 km at the gym."}), None)
    assert "mentioned" in check(ok.model_copy(update={"people": ["Nimal"]}), None)
    assert "empty" in check(dy.Entry(), None)


def fake_lifehub():
    state = {"diary": {}, "items": [], "deleted": [], "posted": []}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else None

        def do_GET(self):
            if self.headers.get("X-API-Key") != "k3y":
                return self._send(401, {"detail": "no"})
            if self.path.startswith("/api/diary/"):
                d = state["diary"].get(self.path.rsplit("/", 1)[1])
                return self._send(200, d) if d else self._send(404, {"detail": "Not found"})
            if self.path == "/api/items?list=diary":
                return self._send(200, state["items"])
            self._send(404, {"detail": "Not found"})

        def do_PUT(self):
            state["diary"][self.path.rsplit("/", 1)[1]] = self._body()
            self._send(200, {"ok": True})

        def do_POST(self):
            state["posted"].append(self._body())
            self._send(200, {"id": 1})

        def do_DELETE(self):
            state["deleted"].append(self.path)
            self._send(200, {"ok": True})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}", state


def setup(tmp_path, monkeypatch, base, config="model: local", extra=""):
    home = tmp_path / "home"
    (home / "Vault").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (tmp_path / ".env").write_text("LIFEHUB_API_KEY=k3y\n", encoding="utf-8")
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\nollama:\n  url: http://127.0.0.1:9\n"
        f"models:\n  tiers:\n    T1: {{provider: ollama, model: '{MODEL}'}}\n" + (extra or "  chain: [T1]\n") +
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: [diary]\n"
        f"  config:\n    diary: {{lifehub_url: '{base}', {config}}}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml")), home / "Vault"


def run(cl, w, workflow, **inp):
    job = cl.post("/jobs", {"plugin": "diary", "workflow": workflow, "input": inp})
    w.run_once(wait=1)
    return wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead", "waiting") and j)


ENTRY = {"title": "Login bug and the gym", "entry": "My day was good. I fixed the login bug and went to the gym with "
         "Kasun.", "highlights": ["fixed the login bug", "gym with Kasun"], "mood": "good", "people": ["Kasun"],
         "places": ["the gym"], "tags": ["work"], "learned": ["Postgres VACUUM frees space"]}


def test_telling_ari_about_the_day_writes_the_entry_locally(tmp_path, monkeypatch):
    base, lh = fake_lifehub()
    argus, vault = setup(tmp_path, monkeypatch, base, config="model: local, vault_copy: true")
    with FakeOllama({MODEL: [{**ENTRY, "entry": "I ran 12 km."}, ENTRY]}) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = run(cl, w, "tell", text=SAID)
        assert r["state"] == "succeeded", r["error"]
        assert "Added to today's diary" in r["result"]["say"] and "Postgres" in r["result"]["say"]
        day = r["result"]["day"]
        e = lh["diary"][day]
        assert e["title"] == "Login bug and the gym" and e["mood"] == "good" and e["people"] == ["Kasun"]
        assert e["said"][0]["text"] == SAID  # your words, kept
        assert "12 km" not in e["entry"]  # the invented number was rejected, the next answer used
        assert lh["posted"] == [{"list": "learn", "name": "Postgres VACUUM frees space",
                                 "notes": "Postgres VACUUM frees space", "store": "diary"}]
        copy = vault / "Diary" / day[:4] / f"{day}.md"
        assert "# Login bug and the gym" in copy.read_text(encoding="utf-8") and SAID in copy.read_text("utf-8")

        r2 = run(cl, w, "tell", text="and in the evening I watched a film")  # more about the same day
        e2 = lh["diary"][day]
        assert [s["text"] for s in e2["said"]] == [SAID, "and in the evening I watched a film"]
        assert len(lh["posted"]) == 1  # the same thing learned isn't sent twice
        assert r2["state"] == "succeeded"

        r3 = run(cl, w, "read", day="today")
        assert r3["result"]["found"] and r3["result"]["title"] == "Login bug and the gym"


def test_claude_writes_it_by_default_and_life_hub_typing_is_taken(tmp_path, monkeypatch):
    base, lh = fake_lifehub()
    cmd = fake_claude(tmp_path, result=json.dumps({**ENTRY, "learned": []}))
    extra = "    T3: {provider: claude}\n  chain: [T1]\n" + f"claude:\n  command: {json.dumps(cmd)}\n"
    argus, _ = setup(tmp_path, monkeypatch, base, config="model: claude", extra=extra)
    lh["items"] = [{"id": 9, "uid": "u9", "list": "diary", "name": "gym with Kasun", "notes": SAID,
                    "created_at": "2026-10-19T14:00:00+00:00", "target_date": None, "deleted": False}]
    with FakeOllama({MODEL: [{}]}) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = run(cl, w, "tidy")
        assert r["state"] == "succeeded", r["error"]
        assert r["result"]["taken"] == 1 and lh["deleted"] == ["/api/items/9"]
        (day,) = r["result"]["days"]
        assert lh["diary"][day]["title"] == "Login bug and the gym" and lh["diary"][day]["model"] == "T3"
        assert SAID in (tmp_path / "claude_stdin.txt").read_text(encoding="utf-8")
        assert not ol.requests  # Claude wrote it: nothing asked of the local models


def test_an_entry_you_edited_keeps_your_text_and_only_gets_the_new_part(tmp_path, monkeypatch):
    base, lh = fake_lifehub()
    argus, _vault = setup(tmp_path, monkeypatch, base)
    day = dy.which_day("today", dt.datetime.now()).isoformat()
    mine = "A slow morning. Fixed the login bug after lunch."
    lh["diary"][day] = {"day": day, "title": "Slow start", "entry": mine, "mood": "okay", "highlights": ["login bug"],
                        "people": [], "places": [], "tags": ["work"], "said": [{"at": "13:00", "text": "fixed the bug"}],
                        "edited_at": "2026-10-22T14:00:00Z"}
    rewritten = {**ENTRY, "title": "Films", "entry": "I watched a film in the evening.", "people": [], "places": [],
                 "learned": []}
    kept = {**rewritten, "entry": mine + "\n\nIn the evening I watched a film."}
    with FakeOllama({MODEL: [rewritten, kept]}) as ol, Server(argus.open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "pc", capabilities=["desktop"], ollama_url=ol.url, watch_folders=False)
        w.register()
        r = run(cl, w, "tell", text="in the evening I watched a film")
        assert r["state"] == "succeeded", r["error"]
        e = lh["diary"][day]
        assert e["entry"].startswith(mine) and e["entry"].endswith("watched a film.")  # the rewrite was refused
        assert e["title"] == "Slow start" and e["highlights"][0] == "login bug"  # your title and list stay
        sent = json.loads(ol.requests[0]["messages"][1]["content"])
        assert sent["your_edited_entry"]["entry"] == mine
