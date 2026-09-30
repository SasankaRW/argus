"""Is Claude logged in? The workers check `claude auth status`; a failed call that says so counts too; the phone
hears once when it goes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from argus.config import load_config
from argus.context import Argus
from argus.models.providers import ClaudeProvider
from conftest import run
from test_worker import Server, client


def make(tmp_path: Path, extra: str = "") -> Argus:
    (tmp_path / "argus.yaml").write_text("logging:\n  file: null\n" + extra, encoding="utf-8")
    return Argus(load_config(tmp_path / "argus.yaml"))


def outbox(a) -> list[dict]:
    return run(a.store.read(lambda c: [json.loads(r[0]) for r in c.execute("SELECT payload FROM outbox")]))


def test_logged_out_is_told_once_and_shown(tmp_path):
    a = make(tmp_path)
    with Server(a.open()) as srv:
        cl = client(srv.url)
        cl.post("/workers/pc/claude", {"logged_in": True, "detail": "oauth_token"})
        assert cl.get("/models")["claude"]["logged_in"] is True
        cl.post("/workers/pc/claude", {"logged_in": False})
        cl.post("/workers/pc/claude", {"logged_in": False})
        assert cl.get("/models")["claude"]["logged_in"] is False
        msgs = [m for m in outbox(a) if m.get("title") == "Claude is logged out"]
        assert len(msgs) == 1 and "claude" in msgs[0]["message"]


def test_a_failed_call_asking_for_login_counts(tmp_path):
    a = make(tmp_path).open()
    run(a.models.register())
    run(a.models.report("T3", False, error="claude exited 1: Invalid API key. Please run /login"))
    assert a.models.claude_auth["logged_in"] is False
    run(a.models.report("T3", False, error="claude gave no answer within 300 s"))
    assert len([m for m in outbox(a) if m.get("title") == "Claude is logged out"]) == 1
    a.store.close()


def test_auth_status_from_the_cli(tmp_path):
    script = tmp_path / "fake_claude_auth.py"
    script.write_text("import json, sys\nprint(json.dumps({'loggedIn': sys.argv[1:] == ['auth', 'status'], "
                      "'authMethod': 'oauth_token'}))\n")
    p = ClaudeProvider([sys.executable, str(script)], [])
    assert p.auth_status() == (True, "oauth_token")
    assert ClaudeProvider([str(tmp_path / "nope")], []).auth_status()[0] is None
