"""Tracker and Life Hub connectors: their APIs with X-API-Key, only the address you set, money private."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.worker import Worker
from argus.worker.runner import network_hosts
from test_worker import Server, client, wait_for

ROOT = Path(__file__).resolve().parents[1]
TICKETS = [{"id": 7, "key": "ACME-12", "title": "Checkout broken", "status": "todo", "priority": 1,
            "due_date": "2026-10-03", "timer_running": False},
           {"id": 8, "key": "SITE-3", "title": "Footer", "status": "done", "priority": 3, "due_date": None}]


class Fake:
    def __init__(self, key: str):
        self.calls: list[tuple[str, str, dict | None]] = []
        me = self

        class H(BaseHTTPRequestHandler):
            def reply(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                me.calls.append((method, self.path, body))
                if self.headers.get("X-API-Key") != key:
                    return self.send(401, {"detail": "no"})
                p = self.path.split("?")[0]
                if p == "/api/summary":
                    return self.send(200, {"open": 1, "urgent": 1, "overdue": 0, "due_week": 1,
                                           "hours_this_month": 12.5, "timer": None,
                                           "focus": [dict(TICKETS[0], due_date="2026-10-03")]})
                if p == "/api/tickets":
                    return self.send(200, TICKETS)
                if p == "/api/tickets/quick":
                    return self.send(200, dict(TICKETS[0], key="ACME-13", title=body["text"]))
                if p.startswith("/api/tickets/7") and method == "PATCH":
                    return self.send(200, dict(TICKETS[0], status=body["status"]))
                if p == "/api/items":
                    return self.send(200, [{"name": "milk", "quantity": "2", "store": "Keells", "status": "to_buy"},
                                           {"name": "eggs", "status": "bought"}])
                if p == "/api/items/quick":
                    return self.send(200, {"name": body["text"], "status": "to_buy", "list": body["list"]})
                if p == "/api/money":
                    return self.send(200, {"connected": True, "home_currency": "LKR", "spare": 41000,
                                           "spare_horizon_days": 12, "month": {"expense": 88000, "budget": 120000}})
                return self.send(404, {"detail": "not found"})

            def send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply("GET")

            def do_POST(self):
                self.reply("POST")

            def do_PATCH(self):
                self.reply("PATCH")

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


def test_network_from_a_setting():
    assert network_hosts(["config:url", "example.com"], {"url": "http://laptop:8080/x"}) == ["laptop", "example.com"]
    assert network_hosts(["config:url"], {}) == []


def test_tracker_and_lifehub(tmp_path, monkeypatch):
    tr, lh = Fake("tk"), Fake("lk")
    (tmp_path / ".env").write_text("TRACKER_API_KEY=tk\nLIFEHUB_API_KEY=lk\n", encoding="utf-8")
    (tmp_path / "argus.yaml").write_text(
        "logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.1\n"
        f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n  live: [tracker, lifehub]\n"
        f"  config:\n    tracker: {{url: '{tr.url}'}}\n    lifehub: {{url: '{lh.url}'}}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with Server(Argus(load_config(tmp_path / "argus.yaml")).open()) as srv:
        cl = client(srv.url)
        w = Worker(cl, "laptop", capabilities=["cpu"], watch_folders=False)
        w.register()
        assert {"tracker", "lifehub"} <= set(w.plugins), w.plugin_errors
        tools = {t["name"]: t for t in cl.get("/tools")}
        assert tools["add_issue"]["risky"] and tools["move_issue"]["risky"] and not tools["list_issues"]["risky"]
        assert tools["money_this_month"].get("private")

        def call(plugin, workflow, **inp):
            job = cl.post("/jobs", {"plugin": plugin, "workflow": workflow, "input": inp})
            w.run_once(wait=1)
            j = wait_for(lambda: (x := cl.get(f"/jobs/{job['id']}"))["state"] in ("succeeded", "dead") and x)
            assert j["state"] == "succeeded", j["error"]
            return j["result"]

        s = call("tracker", "summary")
        assert s["open"] == 1 and s["focus"][0]["key"] == "ACME-12" and s["focus"][0]["priority"] == "P1"
        assert [i["key"] for i in call("tracker", "issues")["issues"]] == ["ACME-12"]  # done ones left out
        assert call("tracker", "issues", project="site", status="done")["issues"][0]["key"] == "SITE-3"
        assert call("tracker", "add", text="ACME bug P1 Login fails @fri")["added"]["key"] == "ACME-13"
        assert call("tracker", "move", key="acme-12", status="done")["moved"]["status"] == "done"
        assert ("PATCH", "/api/tickets/7", {"status": "done"}) in tr.calls
        assert [i["name"] for i in call("lifehub", "lists")["items"]] == ["milk"]
        assert call("lifehub", "add", text="2x bread @keells")["added"]["name"] == "2x bread @keells"
        m = call("lifehub", "money")
        assert m["safe_to_spend"] == 41000 and m["spent_this_month"] == 88000
