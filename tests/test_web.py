"""Web for Ari: SearXNG results as data, pages read as text, public websites only."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import argus.worker.plugins as wplugins
from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
SEARX = {"results": [{"title": "Kandy weather", "url": "https://weather.example/kandy", "content": "29° and showers"},
                     {"title": "bad", "url": "javascript:alert(1)", "content": "x"}]}
PAGE = ("<html><head><title>Kandy forecast</title><script>steal()</script></head><body><nav>menu</nav>"
        "<h1>Kandy</h1><p>Thunderstorms in the afternoon, 29&deg;.</p><footer>cookies</footer></body></html>")


def serve(routes):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            code, body = routes.get(path, (404, ""))
            data = body.encode()
            self.send_response(code)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"


def setup(tmp_path, monkeypatch, searx):
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  config:\n    web: {{searx_url: '{searx}'}}\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return Argus(load_config(tmp_path / "argus.yaml"))


def run(cl, w, workflow, **inp):
    job = cl.post("/jobs", {"plugin": "web", "workflow": workflow, "input": inp})
    w.run_once(wait=1)
    return wait_for(lambda: (j := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and j)


def test_search_then_read_and_local_pages_are_refused(tmp_path, monkeypatch):
    base = serve({"/search": (200, json.dumps(SEARX)), "/page": (200, PAGE)})
    with Server(setup(tmp_path, monkeypatch, base).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "laptop", capabilities=["cpu"], watch_folders=False)
        w.register()
        assert "web" in w.plugins, w.plugin_errors
        tools = {t["name"]: t for t in cl.get("/tools")}
        assert tools["web_search"].get("untrusted") and tools["read_page"].get("untrusted")
        found = run(cl, w, "search", query="kandy weather")["result"]
        assert found["results"] == [{"title": "Kandy weather", "url": "https://weather.example/kandy",
                                     "snippet": "29° and showers"}] and found["untrusted"]
        # the search engine is local, but a page on this machine is not a public website
        bad = run(cl, w, "read", url=base + "/page")
        assert bad["state"] == "dead" and "public" in bad["error"]
        monkeypatch.setattr(wplugins, "public_host", lambda host: True)
        monkeypatch.setattr(wplugins, "public_address", lambda ip: True)
        page = run(cl, w, "read", url=base + "/page")["result"]
        assert page["title"] == "Kandy forecast"
        assert "Thunderstorms in the afternoon, 29°." in page["text"]
        assert "steal" not in page["text"] and "menu" not in page["text"] and "cookies" not in page["text"]


def test_searx_must_be_on_this_machine(tmp_path, monkeypatch):
    with Server(setup(tmp_path, monkeypatch, "https://searx.example.org").open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "laptop", capabilities=["cpu"], watch_folders=False)
        w.register()
        j = run(cl, w, "search", query="x")
        assert j["state"] == "dead" and "127.0.0.1" in j["error"]
