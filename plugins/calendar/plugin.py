"""Your Google Calendar events and holidays for the island's calendar page, read-only.

Every 15 minutes (and on "Refresh now" in Helios) this reads today and the next 30 days from your own calendar
(`primary`) and the Sri Lanka holidays calendar, and keeps the last good answer per calendar in its state in Argus,
so the island works offline. The island asks Argus (`GET /island/calendar`); nothing is shown there until this is
connected, and never an error.

Allowing it, once ("Connect Google Calendar" in Helios): the same flow as the Gmail plugin and the same Google Cloud
OAuth client (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in .env), but its own key with the read-only calendar scope:
Google's "Allow" page opens in your Chrome, you click Allow, and Google sends the OK back to a one-time address on
this PC (127.0.0.1). The key is kept in data/plugins/calendar/token.json (never in Git). Gmail's key is not touched.
Setup: docs/setup.md, "Google Calendar".
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from argus import gcal
from argus.worker import Context, PermanentError, workflow

PLUGIN = "calendar"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 - Google's token address, not a secret
API = "https://www.googleapis.com/calendar/v3/calendars"
LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
OFFLINE = "Couldn't reach Google (offline?): the island keeps showing the last events it got."
PAGES = 4  # up to 1000 events per calendar
_PENDING: dict[str, Any] = {}  # an "Allow" page that is open right now (one at a time)


# ---------------------------------------------------------------- the key (OAuth, once in your Chrome)

def token_path(ctx: Context) -> Path:
    return Path(ctx.data_dir) / "token.json"


def _client(ctx: Context) -> tuple[str, str]:
    cid, sec = ctx.secrets.get("GMAIL_CLIENT_ID"), ctx.secrets.get("GMAIL_CLIENT_SECRET")
    if not cid or not sec:
        raise PermanentError("Google isn't set up yet: put GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in the PC's .env "
                             "(docs/setup.md, Gmail), then enable the Google Calendar API (docs/setup.md, Google "
                             "Calendar)")
    if str(sec).strip().endswith(".apps.googleusercontent.com") or str(cid).strip() == str(sec).strip():
        raise PermanentError("GMAIL_CLIENT_SECRET in the PC's .env is the client ID, not the secret: the secret "
                             "starts with GOCSPX- (Google Cloud, Clients, your client, Client secrets)")
    return cid, sec


def _form(http: Any, data: dict) -> dict:
    status, body = http.request("POST", TOKEN, data=urllib.parse.urlencode(data).encode(),
                                content_type="application/x-www-form-urlencoded")
    got = json.loads(body or b"{}")
    if status >= 400:
        raise PermissionError(got.get("error_description") or got.get("error") or f"HTTP {status}")
    return got


def access_token(ctx: Context) -> str | None:
    """A fresh key from the saved one, or None when Calendar hasn't been allowed yet (or the OK was taken back)."""
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
        page = ("<h2>Done: Argus can read your Google Calendar now.</h2><p>You can close this tab. Your events show "
                "on the island within 15 minutes, or press Refresh now in Helios.</p>" if ok
                else "<h2>Argus didn't get the OK.</h2><p>Press Connect Google Calendar in Helios to try again.</p>")
        body = f"<!doctype html><meta charset=utf-8><title>Argus</title><body style='font-family:sans-serif'>{page}"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())


class _Direct:
    """Google's token address only, not tied to any job (the OK arrives minutes after the job that asked for it
    ended, and ctx.http records every call on that job and refuses to work then)."""

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
        except Exception as e:  # noqa: BLE001 - no OK: "Connect Google Calendar" opens the page again
            try:  # and say why, so it can be read
                path.parent.mkdir(parents=True, exist_ok=True)
                (path.parent / "allow-error.txt").write_text(f"{type(e).__name__}: {e}", encoding="utf-8")
            except OSError:
                pass
        finally:
            srv.server_close()
            flow["done"].set()

    threading.Thread(target=listen, daemon=True, name="calendar-allow").start()
    opener(url, str(ctx.config.get("chrome_profile") or ""))
    return url


# ---------------------------------------------------------------- reading the calendars

def fetch_calendar(ctx: Context, key: str, cal_id: str, days: int) -> dict:
    """One calendar's events, today .. +days: {"events": [...]} or {"error": words, "auth": True when the key was
    refused}."""
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    q = {"timeMin": start.isoformat(), "timeMax": (start + timedelta(days=days + 1)).isoformat(),
         "singleEvents": "true", "orderBy": "startTime", "maxResults": 250,
         "fields": "nextPageToken,items(summary,status,start,end)"}
    kind, items = gcal.kind_of(cal_id), []
    for _ in range(PAGES):
        url = f"{API}/{urllib.parse.quote(cal_id, safe='')}/events?" + urllib.parse.urlencode(q)
        try:
            status, body = ctx.http.request("GET", url, headers={"Authorization": f"Bearer {key}"})
        except OSError:  # offline: the island keeps showing the last good answer
            return {"error": OFFLINE}
        if status >= 400:
            return {"error": gcal.explain(status, body, cal_id), "auth": gcal.needs_new_key(status, body)}
        got = json.loads(body or b"{}")
        items += got.get("items") or []
        if not got.get("nextPageToken"):
            break
        q["pageToken"] = got["nextPageToken"]
    return {"events": gcal.normalize_all(items, kind)}


def calendars_of(ctx: Context) -> list[str]:
    return [str(c).strip() for c in (ctx.config.get("calendars") or gcal.DEFAULT_CALENDARS) if str(c).strip()]


def list_calendars(ctx: Context, key: str) -> list[dict] | None:
    """Your Google Calendar list ({id, selected, hidden}), or None if it can't be read."""
    q = {"minAccessRole": "reader", "fields": "items(id,selected,hidden)", "maxResults": 250}
    try:
        status, body = ctx.http.request("GET", f"{LIST}?{urllib.parse.urlencode(q)}",
                                        headers={"Authorization": f"Bearer {key}"})
    except OSError:
        return None
    return (json.loads(body or b"{}").get("items") or []) if status < 400 else None


def save_status(ctx: Context, state: str, message: str) -> dict:
    status = {"state": state, "message": message, "at": time.time()}
    if ctx.store is not None:
        ctx.store.set("status", status)
    return status


def sync_now(ctx: Context, allow: bool) -> dict:
    """Read every calendar and keep the answers. `allow`: with no key yet, open Google's page (the Connect button)."""
    if ctx.dry_run:
        return {"would_sync": calendars_of(ctx), "dry_run": True}
    try:
        _client(ctx)
    except PermanentError as e:
        save_status(ctx, "error", str(e))
        return {"done": False, "problem": str(e)}
    try:
        key = access_token(ctx)  # not a step: a job's steps are shown in Helios
    except OSError:  # offline while renewing the key
        save_status(ctx, "error", OFFLINE)
        return {"done": False, "problem": OFFLINE}
    if not key:
        if allow:
            start_allow(ctx, getattr(ctx, "open_in_chrome", None) or open_in_chrome)
            who = ctx.config.get("chrome_profile") or "your"
            msg = f"Google's page is open in your Chrome ({who}): click Allow, then press Refresh now."
        elif token_path(ctx).exists():  # a key was saved once and doesn't work any more
            msg = gcal.explain(401, b"{}")
        else:
            msg = "Not connected yet: press Connect Google Calendar."
        save_status(ctx, "needs_connect", msg)
        return {"done": False, "allow": allow, "problem": msg}
    cache = ctx.store.get("cache", {}) if ctx.store is not None else {}
    days = int(ctx.config.get("days") or gcal.HORIZON_DAYS)
    problems = []
    ids = calendars_of(ctx)
    listed = ctx.step("calendar list", list_calendars, ctx, key) if "all" in ids else None
    for cal in gcal.expand_calendars(ids, listed):
        got = ctx.step(f"read {cal}", fetch_calendar, ctx, key, cal, days)
        if got.get("auth"):  # the key was refused or lacks the scope: only a new Allow fixes it
            msg = got["error"]
            if allow:  # the Connect button: open Google's page again
                start_allow(ctx, getattr(ctx, "open_in_chrome", None) or open_in_chrome)
                msg = f"Google's page is open in your Chrome ({ctx.config.get('chrome_profile') or 'your'}): " \
                      "click Allow (tick Calendar), then press Refresh now."
            save_status(ctx, "needs_connect", msg)
            return {"done": False, "allow": allow, "problem": msg}
        cache = gcal.update_cache(cache, cal, got.get("events"), error=got.get("error"))
        if got.get("error"):
            problems.append(got["error"])
    if ctx.store is not None:
        ctx.store.set("cache", cache)
    save_status(ctx, "error" if problems else "ok", problems[0] if problems else "Connected.")
    return {"done": not problems, "events": len(gcal.cached_events(cache)), **({"problem": problems[0]}
                                                                              if problems else {})}


@workflow(PLUGIN, "sync")
def sync(ctx: Context):
    """Every 15 minutes: read the calendars; never opens a browser."""
    return sync_now(ctx, allow=False)


@workflow(PLUGIN, "connect")
def connect(ctx: Context):
    """"Connect Google Calendar" in Helios: with no key yet, opens Google's page in your Chrome; else reads now."""
    return sync_now(ctx, allow=True)
