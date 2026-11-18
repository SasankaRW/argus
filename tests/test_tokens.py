"""Named API tokens: made and revoked one by one, accepted everywhere the worker token is, never stored in the clear."""

from __future__ import annotations

import argparse
import io
import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from argus import tokens as tokens_mod
from argus.api import create_app
from argus.complogs import strip_secrets
from argus.config import load_config
from argus.context import Argus
from argus.logs import redact
from argus.send import cli

W = {"Authorization": "Bearer t0ken"}
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def make(tmp_path, token=True):
    cfg_file = tmp_path / "argus.yaml"
    cfg_file.write_text("logging:\n  file: null\njobs:\n  watchdog_interval_seconds: 0.05\n", encoding="utf-8")
    cfg = load_config(cfg_file)
    if token:
        cfg.secrets.worker_token = "t0ken"
    return Argus(cfg).open()


def mint(c, name="my phone"):
    r = c.post("/tokens", json={"name": name}, headers=W)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_create_list_revoke(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        r = c.post("/tokens", json={"name": "my phone"}, headers=W).json()
        assert r["name"] == "my phone" and r["token"].startswith("ar_") and len(r["token"]) > 40
        assert c.post("/tokens", json={"name": "my phone"}, headers=W).status_code == 409
        rows = c.get("/tokens", headers=W).json()
        assert [x["name"] for x in rows] == ["my phone"] and rows[0]["revoked_at"] is None
        assert "token" not in rows[0] and "hash" not in rows[0] and r["token"] not in json.dumps(rows)
        assert c.delete("/tokens/my%20phone", headers=W).status_code == 200
        assert c.delete("/tokens/my%20phone", headers=W).status_code == 404
        assert c.get("/tokens", headers=W).json()[0]["revoked_at"] is not None
        assert mint(c)  # a revoked name can be made again


def test_only_the_hash_is_stored(tmp_path):
    a = make(tmp_path)
    with TestClient(create_app(a)) as c:
        tok = mint(c)
        rows = a.store.read_sync(lambda conn: conn.execute("SELECT name, hash FROM api_token").fetchall())
        assert [tuple(r) for r in rows] == [("my phone", tokens_mod.digest(tok))]
        dump = a.store.read_sync(lambda conn: [tuple(r) for r in conn.execute("SELECT * FROM api_token")])
        assert tok not in json.dumps(dump, default=str)


def test_named_token_on_guarded_route_events_ws_and_mcp(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        tok = mint(c)
        h = {"Authorization": f"Bearer {tok}"}
        assert c.get("/jobs").status_code == 401
        assert c.get("/jobs", headers=h).status_code == 200
        assert c.get("/jobs", headers={"Authorization": "Bearer ar_" + "x" * 43}).status_code == 401
        assert c.post("/mcp", json=PING).status_code == 401
        assert c.post("/mcp", json=PING, headers=h).status_code == 200
        with c.websocket_connect(f"/ws/events?token={tok}") as ws:
            assert ws.receive_json()["type"] == "hello"
        with c.websocket_connect("/ws/events", headers=h) as ws:
            assert ws.receive_json()["type"] == "hello"
        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws/events?token=nope"):
            pass
        # the approvals decide path takes it as the header token (the phone link's ?t= is a separate check)
        r = c.post("/approvals/missing/decide", json={"answer": "approve"}, headers=h)
        assert r.status_code != 401


def test_revoked_is_401_everywhere(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        tok = mint(c)
        h = {"Authorization": f"Bearer {tok}"}
        assert c.get("/jobs", headers=h).status_code == 200
        assert c.delete("/tokens/my%20phone", headers=W).status_code == 200
        assert c.get("/jobs", headers=h).status_code == 401
        assert c.post("/mcp", json=PING, headers=h).status_code == 401
        assert c.post("/approvals/missing/decide", json={"answer": "approve"}, headers=h).status_code == 401
        with pytest.raises(WebSocketDisconnect), c.websocket_connect(f"/ws/events?token={tok}"):
            pass
        assert c.get("/jobs", headers=W).status_code == 200  # the worker token is untouched


def test_open_websocket_is_cut_when_revoked(tmp_path, monkeypatch):
    from argus.api.routes import events

    monkeypatch.setattr(events, "WS_RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(events, "WS_PING_SECONDS", 0.1)
    with TestClient(create_app(make(tmp_path))) as c:
        tok = mint(c)
        with c.websocket_connect(f"/ws/events?token={tok}") as ws:
            assert ws.receive_json()["type"] == "hello"
            assert c.delete("/tokens/my%20phone", headers=W).status_code == 200
            with pytest.raises(WebSocketDisconnect) as e:
                for _ in range(50):
                    ws.receive_json()
            assert e.value.code == 4401


def test_named_token_cannot_manage_tokens(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        tok = mint(c)
        h = {"Authorization": f"Bearer {tok}"}
        assert c.get("/tokens", headers=h).status_code == 403
        assert c.post("/tokens", json={"name": "x"}, headers=h).status_code == 403
        assert c.delete("/tokens/my%20phone", headers=h).status_code == 403
        assert c.get("/tokens").status_code == 401
        assert c.get("/jobs", headers=h).status_code == 200  # still valid: nothing was revoked


def test_no_worker_token_mode_is_unchanged(tmp_path):
    with TestClient(create_app(make(tmp_path, token=False))) as c:
        assert c.get("/jobs").status_code == 200
        assert c.get("/jobs", headers={"Authorization": "Bearer whatever"}).status_code == 200
        assert c.post("/mcp", json=PING).status_code == 200
        assert c.post("/tokens", json={"name": "a"}).json()["token"]
        assert c.get("/tokens").status_code == 200


def test_last_used_is_written_at_most_once_a_minute(tmp_path):
    a = make(tmp_path)

    def last(conn):
        return conn.execute("SELECT last_used_at FROM api_token").fetchone()[0]

    with TestClient(create_app(a)) as c:
        h = {"Authorization": f"Bearer {mint(c)}"}
        assert a.store.read_sync(last) is None
        c.get("/jobs", headers=h)
        a.store.write_sync(lambda conn: None)  # the touch was queued before this: the writer ran it
        first = a.store.read_sync(last)
        assert first is not None
        c.get("/jobs", headers=h)
        a.store.write_sync(lambda conn: None)
        assert a.store.read_sync(last) == first


def test_revocation_by_another_process_is_seen_within_the_refresh(tmp_path, monkeypatch):
    a = make(tmp_path)
    with TestClient(create_app(a)) as c:
        h = {"Authorization": f"Bearer {mint(c)}"}
        a.store.write_sync(lambda conn: conn.execute("UPDATE api_token SET revoked_at=1"))
        assert c.get("/jobs", headers=h).status_code == 200  # the dict is still fresh
        monkeypatch.setattr(tokens_mod, "REFRESH", -1.0)
        assert c.get("/jobs", headers=h).status_code == 401


def test_tokens_are_redacted_in_logs():
    tok = tokens_mod.PREFIX + "Ab1-" * 11
    assert tok not in redact(f"GET /ws/events?token={tok} 101")
    assert tok not in redact(f"saw {tok} here")
    assert tok not in strip_secrets(f"saw {tok} here")


def test_cli_token_commands(tmp_path, capsys):
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=0):
        calls.append((req.method, req.full_url, req.get_header("Authorization")))
        return Resp(json.dumps([] if req.method == "GET" else {"name": "n", "token": "ar_x"}).encode())

    (tmp_path / "a.yaml").write_text("server:\n  port: 8601\n", encoding="utf-8")
    cfg = load_config(tmp_path / "a.yaml")
    cfg.secrets.worker_token = "t0ken"
    ns = argparse.Namespace
    assert cli.token_command(cfg, ns(token_cmd="new", name="n"), opener) == 0
    assert cli.token_command(cfg, ns(token_cmd="list"), opener) == 0
    assert cli.token_command(cfg, ns(token_cmd="revoke", name="my phone"), opener) == 0
    assert calls == [
        ("POST", "http://127.0.0.1:8601/tokens", "Bearer t0ken"),
        ("GET", "http://127.0.0.1:8601/tokens", "Bearer t0ken"),
        ("DELETE", "http://127.0.0.1:8601/tokens/my%20phone", "Bearer t0ken")]
    assert "ar_x" in capsys.readouterr().out



def test_revoke_trims_the_name_like_create(tmp_path):
    with TestClient(create_app(make(tmp_path))) as c:
        tok = mint(c, "laptop")
        assert c.get("/status", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
        assert c.delete("/tokens/%20laptop%20", headers=W).status_code == 200
        assert c.get("/tokens", headers=W).json()[0]["revoked_at"] is not None
