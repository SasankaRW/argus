"""Argus as an MCP server: JSON-RPC over POST /mcp, the Helios token, a small read-mostly toolset."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from test_ask import make
from test_worker import Server


def rpc(url: str, body, token: str | None = "tok"):
    req = urllib.request.Request(url + "/mcp", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json, text/event-stream",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def tool(url, name, args=None):
    _, r = rpc(url, {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name,
                                                                                  "arguments": args or {}}})
    res = r["result"]
    return res["isError"], (json.loads(res["content"][0]["text"]) if not res["isError"] else res["content"][0]["text"])


def test_handshake_tools_and_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGUS_WORKER_TOKEN", "tok")
    a = make(tmp_path, monkeypatch)
    with Server(a.open()) as srv:
        u = srv.url
        with pytest.raises(urllib.error.HTTPError) as e:
            rpc(u, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, token=None)
        assert e.value.code == 401
        st, r = rpc(u, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "1"}}})
        assert r["result"]["protocolVersion"] == "2025-03-26" and "tools" in r["result"]["capabilities"]
        assert rpc(u, {"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
        names = {t["name"] for t in rpc(u, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[1]["result"]["tools"]}
        assert {"argus_status", "get_job", "run_button", "list_approvals"} <= names
        assert not any("approve" == n or n.startswith("power") for n in names)

        bad, status = tool(u, "argus_status")
        assert not bad and status["status"] in ("ok", "degraded")
        bad, buttons = tool(u, "list_buttons")
        assert "run:downloads-organizer:sort" in {b["id"] for b in buttons}
        bad, started = tool(u, "run_button", {"id": "run:downloads-organizer:sort"})
        assert not bad and started["job_id"]
        bad, job = tool(u, "get_job", {"id": started["job_id"]})
        assert job["plugin"] == "downloads-organizer" and job["state"] == "queued"
        assert tool(u, "run_button", {"id": "power:shutdown"})[0] is True  # not from here
        assert tool(u, "get_job", {"id": "nope"})[0] is True
        bad, jobs = tool(u, "list_jobs", {"limit": 5})
        assert jobs[0]["id"] == started["job_id"]
        assert tool(u, "ask_argus", {"text": "what's running?"})[1]["action"] == "show:queue"
        _, r = rpc(u, {"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
        assert r["error"]["code"] == -32601
