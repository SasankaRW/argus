"""Your Gmail, read-only, through Google's Gmail API: "check my mails" says who wrote and about what, "read the
second one" reads it out.

Allowing it, once: Ari opens Google's "Allow" page in your own Chrome (the profile in `chrome_profile`, Sasanka by
default), you click Allow, and Google sends the OK back to a one-time address on this PC (127.0.0.1). Argus keeps
only the resulting key (data/plugins/gmail/token.json, never in Git) and can read mail, nothing else: no sending,
no deleting, and reading doesn't mark mail as read. Your mail is never sent to Claude (the tools are private).

Needs a Google Cloud OAuth client of type "Desktop app" with the Gmail API on: its id and secret go in .env as
GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET (docs/setup.md, "Gmail").
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from argus.worker import Context, PermanentError, workflow

PLUGIN = "gmail"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 - Google's token address, not a secret
API = "https://gmail.googleapis.com/gmail/v1/users/me"
ORDINALS = {"first": 0, "1st": 0, "one": 0, "latest": 0, "newest": 0, "top": 0, "second": 1, "2nd": 1, "two": 1,
            "third": 2, "3rd": 2, "three": 2, "fourth": 3, "4th": 3, "four": 3, "fifth": 4, "5th": 4, "five": 4}
_PENDING: dict[str, Any] = {}  # an "Allow" page that is open right now (one at a time)


# ---------------------------------------------------------------- picking and reading (plain code, tested)

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def pick_mail(mails: list[dict], which: str) -> dict | None:
    """The mail you meant: "1" / "the second one" / "last" / "from Kaancha" / words of its subject."""
    if not mails:
        return None
    w = _norm(which)
    w = re.sub(r"^(?:the|my|me)\s+", "", w)
    w = re.sub(r"\s+(?:one|e ?mail|mail|message)s?$", "", w).strip()
    if w in ("", "it", "that", "this"):
        return mails[0]
    if w.isdigit():
        n = int(w) - 1
        return mails[n] if 0 <= n < len(mails) else None
    if w in ("last", "oldest", "bottom"):
        return mails[-1]
    if w in ORDINALS:
        n = ORDINALS[w]
        return mails[n] if n < len(mails) else None
    w = re.sub(r"^(?:from|by|about|called|titled|on)\s+", "", w)
    words = [x for x in w.split() if len(x) > 1 and x not in ("the", "an", "my", "that", "this", "one", "email",
                                                              "mail", "message")]
    for m in mails:  # the sender first, then the subject
        if words and all(x in _norm(f"{m['from']} {m.get('email', '')}") for x in words):
            return m
    for m in mails:
        if words and all(x in _norm(m["subject"]) for x in words):
            return m
    return None


def headers_of(msg: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers") or []}


def sender(value: str) -> tuple[str, str]:
    """'"Kaancha Perera" <kaancha@example.com>' -> ("Kaancha Perera", "kaancha@example.com")."""
    m = re.match(r'\s*"?([^"<]*?)"?\s*<([^>]+)>', value or "")
    if m:
        return (m.group(1).strip() or m.group(2)), m.group(2).strip()
    v = (value or "").strip()
    return v or "someone", v if "@" in v else ""


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def body_text(payload: dict) -> str:
    """The mail's words: its plain-text part, else its HTML without the tags; quoted older mail left out."""
    plain, rich = [], []

    def walk(p: dict) -> None:
        mime = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            rich.append(_b64(data))
        for c in p.get("parts") or []:
            walk(c)

    walk(payload)
    if plain:
        text = "\n".join(plain)
    else:
        t = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", "\n".join(rich))
        t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
        text = html.unescape(re.sub(r"<[^>]+>", " ", t))
        text = re.sub(r"\n\s*\n+", "\n", text)  # paragraphs from the HTML: one line each
    keep = []
    for line in text.splitlines():
        if re.match(r"\s*>", line) or re.match(r"\s*On .{5,120} wrote:\s*$", line):
            break  # the older mail it replies to
        keep.append(line.strip())
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"[ \t]+([.,!?;:])", r"\1", out)


# ---------------------------------------------------------------- the key (OAuth, once in your Chrome)

def token_path(ctx: Context) -> Path:
    return Path(ctx.data_dir) / "token.json"


def _client(ctx: Context) -> tuple[str, str]:
    cid, sec = ctx.secrets.get("GMAIL_CLIENT_ID"), ctx.secrets.get("GMAIL_CLIENT_SECRET")
    if not cid or not sec:
        raise PermanentError("Gmail isn't set up yet: put GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in the PC's .env "
                             "(docs/setup.md, Gmail)")
    return cid, sec


def _form(http: Any, data: dict) -> dict:
    status, body = http.request("POST", TOKEN, data=urllib.parse.urlencode(data).encode(),
                                content_type="application/x-www-form-urlencoded")
    got = json.loads(body or b"{}")
    if status >= 400:
        raise PermissionError(got.get("error_description") or got.get("error") or f"HTTP {status}")
    return got


def access_token(ctx: Context) -> str | None:
    """A fresh key from the saved one, or None when Gmail hasn't been allowed yet (or the OK was taken back)."""
    p = token_path(ctx)
    try:
        tok = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if tok.get("access_token") and float(tok.get("expires_at") or 0) > time.time() + 60:
        return str(tok["access_token"])
    if not tok.get("refresh_token"):
        return None
    cid, sec = _client(ctx)
    try:
        got = _form(ctx.http, {"client_id": cid, "client_secret": sec, "refresh_token": tok["refresh_token"],
                               "grant_type": "refresh_token"})
    except PermissionError:  # you took the OK back, or it expired: allow it again
        return None
    tok.update(access_token=got["access_token"], expires_at=time.time() + float(got.get("expires_in") or 3600))
    p.write_text(json.dumps(tok), encoding="utf-8")
    return str(tok["access_token"])


def chrome_and_profile(name: str) -> tuple[str | None, str | None]:  # pragma: no cover - Windows, your Chrome
    """chrome.exe and the folder of the profile called `name` (its name or its Google account's email)."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    exe = next((str(p) for p in (Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) /
                                 "Google/Chrome/Application/chrome.exe",
                                 Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) /
                                 "Google/Chrome/Application/chrome.exe",
                                 Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe")
                if p.is_file()), None)
    try:
        cache = json.loads((base / "Local State").read_text(encoding="utf-8"))["profile"]["info_cache"]
    except (OSError, ValueError, KeyError):
        return exe, None
    want = name.strip().lower()
    for folder, info in cache.items():
        if want and want in (str(info.get("name", "")).lower(), str(info.get("user_name", "")).lower(),
                             str(info.get("gaia_name", "")).lower()):
            return exe, folder
    return exe, None


def open_in_chrome(url: str, profile: str) -> None:  # pragma: no cover - opens your Chrome
    exe, folder = chrome_and_profile(profile)
    if exe:
        subprocess.Popen([exe, *([f"--profile-directory={folder}"] if folder else []), url])  # noqa: S603
    else:
        import webbrowser

        webbrowser.open(url)


class _Back(BaseHTTPRequestHandler):
    """The one-time address Google sends the OK to (127.0.0.1 only)."""

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        flow = _PENDING.get("flow")
        if flow is not None and q.get("state", [""])[0] == flow["state"]:
            flow["code"] = q.get("code", [""])[0] or None
            flow["error"] = q.get("error", [""])[0] or None
            flow["done"].set()
        ok = flow is not None and flow.get("code")
        page = ("<h2>Done: Ari can read your Gmail now.</h2><p>You can close this tab and ask Ari again.</p>" if ok
                else "<h2>Ari didn't get the OK.</h2><p>Ask Ari to check your mail to try again.</p>")
        body = f"<!doctype html><meta charset=utf-8><title>Ari</title><body style='font-family:sans-serif'>{page}"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


class _Direct:
    """Google's token address only, not tied to any job. The OK arrives minutes after the job that asked for it has
    ended, and ctx.http (which records every call on that job) refuses to work then."""

    def request(self, method: str, url: str, *, data: bytes | None = None, content_type: str | None = None,
                **_: Any) -> tuple[int, bytes]:
        if not url.startswith(TOKEN):
            raise PermissionError("only Google's token address")
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - fixed https address
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


def start_allow(ctx: Context, opener=open_in_chrome, wait_s: float = 600) -> str:
    """Open Google's "Allow" page in your Chrome and keep listening (in the background, up to 10 minutes) for its
    OK; the key is saved when it comes. Returns the address opened."""
    cid, sec = _client(ctx)
    old = _PENDING.get("flow")
    if old is not None and not old["done"].is_set() and time.time() < old["until"]:
        opener(old["url"], str(ctx.config.get("chrome_profile") or ""))  # still waiting: show the page again
        return old["url"]
    srv = HTTPServer(("127.0.0.1", 0), _Back)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    url = AUTH + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"})
    flow = {"url": url, "state": state, "done": threading.Event(), "code": None, "error": None,
            "until": time.time() + wait_s}
    _PENDING["flow"] = flow
    http, path = getattr(ctx, "after_job_http", None) or _Direct(), token_path(ctx)

    def listen() -> None:
        srv.timeout = 1.0
        try:
            while not flow["done"].is_set() and time.time() < flow["until"]:
                srv.handle_request()
            if flow["code"]:
                got = _form(http, {"client_id": cid, "client_secret": sec, "code": flow["code"],
                                   "code_verifier": verifier, "redirect_uri": redirect,
                                   "grant_type": "authorization_code"})
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"refresh_token": got.get("refresh_token"),
                                           "access_token": got.get("access_token"),
                                           "expires_at": time.time() + float(got.get("expires_in") or 3600)}),
                               encoding="utf-8")
                os.replace(tmp, path)  # never half a file
        except Exception as e:  # noqa: BLE001 - no OK: the next "check my mail" opens the page again
            try:  # and say why, so it can be read
                path.parent.mkdir(parents=True, exist_ok=True)
                (path.parent / "allow-error.txt").write_text(f"{type(e).__name__}: {e}", encoding="utf-8")
            except OSError:
                pass
        finally:
            srv.server_close()
            flow["done"].set()

    threading.Thread(target=listen, daemon=True, name="gmail-allow").start()
    opener(url, str(ctx.config.get("chrome_profile") or ""))
    return url


# ---------------------------------------------------------------- the mail

def _get(ctx: Context, key: str, path: str) -> dict:
    status, body = ctx.http.request("GET", API + path, headers={"Authorization": f"Bearer {key}"})
    if status == 401:
        raise PermissionError("the key was refused")
    if status >= 400:
        raise RuntimeError(f"Gmail answered {status}")
    return json.loads(body or b"{}")


def _needs_allow(ctx: Context) -> dict:
    start_allow(ctx, getattr(ctx, "open_in_chrome", None) or open_in_chrome)
    who = str(ctx.config.get("chrome_profile") or "your")
    return {"done": False, "allow": True,
            "problem": f"I need your OK once: I've opened Google's page in your Chrome ({who}). Click Allow, then "
                       "ask me again"}


def unread(ctx: Context, key: str) -> list[dict]:
    n = int(ctx.config.get("how_many") or 10)
    got = _get(ctx, key, "/messages?" + urllib.parse.urlencode({"q": "is:unread in:inbox", "maxResults": n}))
    out = []
    for m in got.get("messages") or []:
        msg = _get(ctx, key, f"/messages/{m['id']}?format=metadata&metadataHeaders=From&metadataHeaders=Subject"
                             "&metadataHeaders=Date")
        h = headers_of(msg)
        name, email = sender(h.get("from", ""))
        out.append({"id": m["id"], "from": name, "email": email, "subject": h.get("subject", "").strip() or
                    "(no subject)", "snippet": html.unescape(msg.get("snippet") or ""), "when": h.get("date", "")})
    return out


@workflow(PLUGIN, "new")
def new(ctx: Context):
    """"Check my mails": your unread inbox, newest first."""
    if ctx.dry_run:
        return {"would_check": "gmail", "dry_run": True}
    try:
        _client(ctx)
    except PermanentError as e:  # not set up yet: say so, plainly
        return {"done": False, "problem": str(e)}
    key = ctx.step("key", access_token, ctx)
    if not key:
        return ctx.step("allow", _needs_allow, ctx)
    try:
        mails = ctx.step("unread", unread, ctx, key)
    except PermissionError:
        return ctx.step("allow", _needs_allow, ctx)
    if ctx.store is not None:
        ctx.store.set("mail", mails[:20])
    return {"done": True, "count": len(mails),
            "mails": [{"n": i + 1, "from": m["from"], "subject": m["subject"], "snippet": m["snippet"][:160],
                       "when": m["when"]} for i, m in enumerate(mails)]}


@workflow(PLUGIN, "read")
def read(ctx: Context):
    """"Read the second one" / "what does the mail from Kaancha say": that mail's words."""
    which = str(ctx.input.get("which") or "").strip()
    if ctx.dry_run:
        return {"would_read": which or "the newest", "dry_run": True}
    try:
        _client(ctx)
    except PermanentError as e:  # not set up yet: say so, plainly
        return {"done": False, "problem": str(e)}
    key = ctx.step("key", access_token, ctx)
    if not key:
        return ctx.step("allow", _needs_allow, ctx)
    mails = list((ctx.store.get("mail", []) if ctx.store is not None else []) or [])
    m = pick_mail(mails, which)
    try:
        if m is None:  # not in the last list: look again
            mails = ctx.step("unread", unread, ctx, key)
            if ctx.store is not None:
                ctx.store.set("mail", mails[:20])
            m = pick_mail(mails, which)
        if m is None:
            return {"done": False, "problem": f"I can't find {('an email ' + which) if which else 'a new email'}"}
        msg = ctx.step("open", _get, ctx, key, f"/messages/{m['id']}?format=full")
    except PermissionError:
        return ctx.step("allow", _needs_allow, ctx)
    text = body_text(msg.get("payload") or {})
    return {"done": True, "from": m["from"], "subject": m["subject"], "text": text[:4000] or m.get("snippet", ""),
            "whole": bool(text)}
