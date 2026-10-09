"""Your Gmail, read-only: allowing it once in your Chrome (a fake Google here), the unread list, one mail read out,
and how Ari says it."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus.worker import think as th
from argus.worker.think import straight_to

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("t_gmail", ROOT / "plugins" / "gmail" / "plugin.py")
gm = importlib.util.module_from_spec(spec)
sys.modules["t_gmail"] = gm
spec.loader.exec_module(gm)


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


MAILS = {
    "a1": {"From": '"Kaancha Perera" <kaancha@example.com>', "Subject": "Dinner on Friday?",
           "snippet": "Are we still on for Friday at 7?",
           "payload": {"mimeType": "text/plain", "body": {"data": b64(
               "Hi Sas,\nAre we still on for Friday at 7? I booked the place.\n\n"
               "On Mon, 6 Oct 2026 Sas wrote:\n> yes")}}},
    "b2": {"From": "HNB Alerts <alerts@hnb.lk>", "Subject": "Your statement is ready",
           "snippet": "Your October statement &amp; summary",
           "payload": {"mimeType": "multipart/alternative", "parts": [
               {"mimeType": "text/html", "body": {"data": b64(
                   "<html><style>p{}</style><p>Dear customer,</p><p>Your statement is <b>ready</b>.</p></html>")}}]}},
}


class FakeGoogle:
    """Google's token and Gmail addresses, as ctx.http sees them."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.refused = False

    def request(self, method, url, *, data=None, headers=None, content_type=None, **kw):
        form = dict(urllib.parse.parse_qsl(data.decode())) if data else {}
        self.calls.append((method, url, form))
        if url == gm.TOKEN:
            if form.get("grant_type") == "authorization_code":
                assert form["code"] == "C0DE" and form["code_verifier"]
                return 200, json.dumps({"access_token": "A1", "refresh_token": "R1", "expires_in": 3600}).encode()
            assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "R1"
            return 200, json.dumps({"access_token": "A2", "expires_in": 3600}).encode()
        if self.refused:
            return 401, b"{}"
        assert headers["Authorization"].startswith("Bearer A")
        path = url[len(gm.API):]
        if path.startswith("/messages?"):
            assert "is%3Aunread" in path
            return 200, json.dumps({"messages": [{"id": "a1"}, {"id": "b2"}]}).encode()
        mid = path.split("/")[2].split("?")[0]
        m = MAILS[mid]
        msg = {"id": mid, "snippet": m["snippet"],
               "payload": {**m["payload"], "headers": [{"name": "From", "value": m["From"]},
                                                        {"name": "Subject", "value": m["Subject"]}]}}
        return 200, json.dumps(msg).encode()


def ctx(tmp_path, inp=None, *, secrets_ok=True, token=None, google=None):
    st: dict = {}
    if token is not None:
        (tmp_path / "token.json").write_text(json.dumps(token))
    env = {"GMAIL_CLIENT_ID": "cid.apps.googleusercontent.com", "GMAIL_CLIENT_SECRET": "sec"} if secrets_ok else {}
    return SimpleNamespace(
        input=inp or {}, dry_run=False, config={"chrome_profile": "Sasanka", "how_many": 10}, data_dir=tmp_path,
        secrets=SimpleNamespace(get=lambda k, d=None: env.get(k, d)), http=google or FakeGoogle(),
        store=SimpleNamespace(get=lambda k, d=None: st.get(k, d), set=lambda k, v: st.__setitem__(k, v), data=st),
        step=lambda name, fn, *a, **k: fn(*a, **k))


GOOD = {"access_token": "A1", "refresh_token": "R1", "expires_at": time.time() + 3000}


def test_check_my_mails_lists_the_unread_ones(tmp_path):
    c = ctx(tmp_path, token=GOOD)
    out = gm.new(c)
    assert out["done"] and out["count"] == 2
    assert [(m["from"], m["subject"]) for m in out["mails"]] == [("Kaancha Perera", "Dinner on Friday?"),
                                                                ("HNB Alerts", "Your statement is ready")]
    assert out["mails"][1]["snippet"] == "Your October statement & summary"
    said = th.mail_said("check_email", out)
    assert said == ("You've got 2 new emails. 1, from Kaancha Perera, about Dinner on Friday?; 2, from HNB Alerts, "
                    "about Your statement is ready. Want me to read one? Say which.")
    assert th.mail_follow_up("check_email", out) == {"kind": "tool", "name": "read_email", "args": {"which": "1"}}


def test_read_one_out(tmp_path):
    c = ctx(tmp_path, {"which": "from Kaancha"}, token=GOOD)
    gm.new(c)
    out = gm.read(c)
    assert out["done"] and out["text"] == "Hi Sas,\nAre we still on for Friday at 7? I booked the place."  # no quote
    assert th.mail_said("read_email", out) == ("From Kaancha Perera, Dinner on Friday?. It says: Hi Sas, Are we still "
                                               "on for Friday at 7? I booked the place.")
    c.input = {"which": "second"}
    assert gm.read(c)["text"] == "Dear customer,\nYour statement is ready."
    c.input = {"which": "from nobody"}
    assert gm.read(c) == {"done": False, "problem": "I can't find an email from nobody"}


def test_an_old_key_is_refreshed(tmp_path):
    g = FakeGoogle()
    c = ctx(tmp_path, token={**GOOD, "access_token": "OLD", "expires_at": time.time() - 10}, google=g)
    assert gm.new(c)["done"]
    assert g.calls[0][2]["grant_type"] == "refresh_token"
    assert json.loads((tmp_path / "token.json").read_text())["access_token"] == "A2"


def test_allowing_it_once_in_your_chrome(tmp_path):
    g = FakeGoogle()
    opened: list[tuple[str, str]] = []
    c = ctx(tmp_path, google=g)
    c.after_job_http = g
    c.open_in_chrome = lambda url, profile: opened.append((url, profile))
    gm._PENDING.clear()
    out = gm.new(c)
    assert not out["done"] and out["allow"] and "Click Allow" in out["problem"] and "Sasanka" in out["problem"]
    url, profile = opened[0]
    assert profile == "Sasanka"
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["scope"] == gm.SCOPE and q["code_challenge_method"] == "S256" and q["access_type"] == "offline"
    assert q["redirect_uri"].startswith("http://127.0.0.1:")
    # you click Allow: Google sends the browser back to the one-time address with the code
    page = urllib.request.urlopen(f"{q['redirect_uri']}/?state={q['state']}&code=C0DE").read().decode()
    assert "Ari can read your Gmail now" in page
    for _ in range(100):
        if (tmp_path / "token.json").exists():
            break
        time.sleep(0.05)
    assert json.loads((tmp_path / "token.json").read_text())["refresh_token"] == "R1"
    assert gm.new(c)["count"] == 2  # and now it reads


def test_the_ok_is_saved_after_the_job_that_asked_has_ended(tmp_path):
    """Google's answer comes minutes later: ctx.http (tied to the finished job) must not be needed then."""
    class Ended:
        def request(self, *a, **k):
            raise RuntimeError("the job has ended")

    g = FakeGoogle()
    c = ctx(tmp_path, google=Ended())
    c.after_job_http = g
    opened: list[tuple[str, str]] = []
    c.open_in_chrome = lambda url, profile: opened.append((url, profile))
    gm._PENDING.clear()
    gm.new(c)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0][0]).query))
    urllib.request.urlopen(f"{q['redirect_uri']}/?state={q['state']}&code=C0DE").read()
    for _ in range(100):
        if (tmp_path / "token.json").exists():
            break
        time.sleep(0.05)
    assert json.loads((tmp_path / "token.json").read_text())["refresh_token"] == "R1"


def test_a_failed_exchange_leaves_the_reason(tmp_path):
    class Refuses:
        def request(self, *a, **k):
            return 400, b'{"error": "invalid_grant", "error_description": "bad code"}'

    c = ctx(tmp_path)
    c.after_job_http = Refuses()
    opened: list[tuple[str, str]] = []
    c.open_in_chrome = lambda url, profile: opened.append((url, profile))
    gm._PENDING.clear()
    gm.new(c)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0][0]).query))
    urllib.request.urlopen(f"{q['redirect_uri']}/?state={q['state']}&code=BAD").read()
    for _ in range(100):
        if (tmp_path / "allow-error.txt").exists() and (tmp_path / "allow-error.txt").read_text():
            break
        time.sleep(0.05)
    assert "bad code" in (tmp_path / "allow-error.txt").read_text()
    assert not (tmp_path / "token.json").exists()


def test_a_wrong_answer_to_the_address_is_ignored(tmp_path):
    c = ctx(tmp_path)
    opened: list = []
    c.open_in_chrome = lambda url, profile: opened.append(url)
    gm._PENDING.clear()
    gm.new(c)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))
    page = urllib.request.urlopen(f"{q['redirect_uri']}/?state=forged&code=EVIL").read().decode()
    assert "didn't get the OK" in page and not (tmp_path / "token.json").exists()
    gm._PENDING["flow"]["until"] = 0  # stop listening


def test_not_set_up_yet_is_said_plainly(tmp_path):
    out = gm.new(ctx(tmp_path, secrets_ok=False))
    assert not out["done"] and "GMAIL_CLIENT_ID" in out["problem"]


def test_the_client_id_pasted_as_the_secret_is_said_plainly(tmp_path):
    c = ctx(tmp_path)
    c.secrets = SimpleNamespace(get=lambda k, d=None: "123-abc.apps.googleusercontent.com")
    out = gm.new(c)
    assert not out["done"] and "GOCSPX" in out["problem"] and not (tmp_path / "token.json").exists()


def test_a_key_google_refuses_asks_again(tmp_path):
    g = FakeGoogle()
    g.refused = True
    c = ctx(tmp_path, token=GOOD, google=g)
    c.open_in_chrome = lambda url, profile: None
    gm._PENDING.clear()
    assert gm.new(c)["allow"]
    gm._PENDING["flow"]["until"] = 0


@pytest.mark.parametrize("said,tool,args", [
    ("check my mails", "check_email", {}),
    ("Open Chrome and check my emails", "check_email", {}),
    ("Can you open Chrome and check for my emails", "check_email", {}),
    ("any new emails?", "check_email", {}),
    ("tell me what new mails I got", "check_email", {}),
    ("what's in my inbox", "check_email", {}),
    ("read the second one", "read_email", {"which": "second"}),
    ("what does the mail from Kaancha say", "read_email", {"which": "from Kaancha"}),
    ("open the email about the invoice", "read_email", {"which": "about the invoice"}),
    ("read email 3", "read_email", {"which": "3"}),
])
def test_said_plainly(said, tool, args):
    tools = {t: {} for t in ("check_email", "read_email", "do_in_browser", "open_app", "web_search")}
    assert straight_to(tools, said) == (tool, args)


def test_the_manifest_loads_and_the_tools_are_private(tmp_path):
    from argus.config import load_config
    from argus.plugins import PluginHost

    (tmp_path / "argus.yaml").write_text(f"plugins:\n  dirs: ['{(ROOT / 'plugins').as_posix()}']\n")
    host = PluginHost(load_config(tmp_path / "argus.yaml"))
    host.load()
    assert not host.errors, host.errors
    m = host.plugins["gmail"].manifest
    assert {t.name: t.private for t in m.ari.tools} == {"check_email": True, "read_email": True}
    assert m.job_needs() == ["desktop", "session"]
