"""PC apps: Ari opens apps, files, folders and websites in your Windows session, and closes apps (after your yes).

Apps are found by name the way the Start menu does: every Start-menu shortcut and every installed Store app
(`Get-StartApps`), matched loosely ("vs code" -> "Visual Studio Code", "spotify" -> "Spotify"). Closing asks the app
to close like the X button (it may ask you to save); nothing is killed.

Runs only on a worker in your logged-in session (capability `session`): a program started at boot, before you log
in, couldn't show you a window.
"""

from __future__ import annotations

import difflib
import json
import os
import platform
import re
import subprocess
import urllib.parse

from argus.worker import Context, PermanentError, workflow

PLUGIN = "pc-apps"
WIN = platform.system() == "Windows"
ALIASES = {"vscode": "visual studio code", "vs code": "visual studio code", "code": "visual studio code",
           "word": "word", "excel": "excel", "browser": "chrome", "files": "file explorer",
           "explorer": "file explorer", "terminal": "terminal", "settings": "settings", "calc": "calculator",
           "task manager": "task manager", "notepad": "notepad", "paint": "paint", "camera": "camera",
           "what's up": "whatsapp", "whats up": "whatsapp", "whatsapp": "whatsapp"}


def _ps(script: str, timeout: float = 20) -> str:
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PermanentError(f"PowerShell: {e}") from None
    if p.returncode != 0:
        raise PermanentError((p.stderr or p.stdout).strip()[:300] or "PowerShell failed")
    return p.stdout


def installed() -> list[dict]:
    """[{name, id}]: Start-menu apps (Store apps and shortcuts), from Get-StartApps."""
    if not WIN:
        raise PermanentError("opening apps works on Windows only")
    out = _ps("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | Select-Object Name, AppID | "
              "ConvertTo-Json -Compress")
    rows = json.loads(out or "[]")
    rows = rows if isinstance(rows, list) else [rows]
    return [{"name": r["Name"], "id": r["AppID"]} for r in rows if r.get("Name") and r.get("AppID")]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def best_match(asked: str, apps: list[dict]) -> dict | None:
    """The app you meant: exact name, then starts-with / contains, then close spelling. None when unsure."""
    want = _norm(ALIASES.get(_norm(asked), asked))
    if not want:
        return None
    names = {_norm(a["name"]): a for a in apps}
    if want in names:
        return names[want]
    starts = sorted((a for n, a in names.items() if n.startswith(want)), key=lambda a: len(a["name"]))
    if starts:
        return starts[0]
    words = set(want.split())
    contains = sorted((a for n, a in names.items() if words <= set(n.split())), key=lambda a: len(a["name"]))
    if contains:
        return contains[0]
    close = difflib.get_close_matches(want, list(names), n=1, cutoff=0.75)
    return names[close[0]] if close else None


@workflow(PLUGIN, "list_apps")
def list_apps(ctx: Context):
    return {"apps": sorted({a["name"] for a in ctx.step("list", installed)})}


@workflow(PLUGIN, "open_app")
def open_app(ctx: Context):
    asked = str(ctx.input.get("name") or "").strip()
    if not asked:
        raise PermanentError("which app?")

    def go():
        app = best_match(asked, installed())
        if app is None:
            return {"opened": None, "problem": f"I can't find an app called {asked!r} on the PC"}
        if ctx.dry_run:
            return {"would_open": app["name"], "dry_run": True}
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['id']}"])  # like clicking it in Start
        return {"opened": app["name"]}

    out = ctx.step("open", go)
    if out.get("opened"):
        ctx.saved(5, key="opened")
    return out


@workflow(PLUGIN, "open_path")
def open_path(ctx: Context):
    raw = str(ctx.input.get("path") or "").strip().strip('"')
    if not raw:
        raise PermanentError("which file or folder?")

    def go():
        path = os.path.expandvars(os.path.expanduser(raw))
        if not ctx.files.exists(path):  # also checks it is somewhere Argus may look
            return {"opened": None, "problem": f"{raw} doesn't exist"}
        if ctx.dry_run:
            return {"would_open": path, "dry_run": True}
        if not WIN:
            raise PermanentError("opening files works on Windows only")
        os.startfile(path)  # type: ignore[attr-defined]  # its usual app, like a double-click
        return {"opened": path}

    return ctx.step("open", go)


@workflow(PLUGIN, "open_url")
def open_url(ctx: Context):
    raw = str(ctx.input.get("url") or "").strip()
    if not raw:
        raise PermanentError("which site?")
    if re.match(r"^https?://", raw, re.I):
        url = raw
    elif re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", raw, re.I):
        url = "https://" + raw
    else:
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(raw)

    def go():
        if ctx.dry_run:
            return {"would_open": url, "dry_run": True}
        if not WIN:
            raise PermanentError("opening websites works on Windows only")
        os.startfile(url)  # type: ignore[attr-defined]  # the default browser
        return {"opened": url}

    return ctx.step("open", go)


def whatsapp_url(text: str, phone: str = "") -> str:
    """whatsapp:// link that opens WhatsApp with the message typed (to that number, or WhatsApp asks which chat)."""
    digits = re.sub(r"\D", "", phone)
    q = {"text": text}
    if 8 <= len(digits) <= 15:
        q = {"phone": digits, **q}
    return "whatsapp://send?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


@workflow(PLUGIN, "whatsapp")
def whatsapp(ctx: Context):
    to = str(ctx.input.get("to") or "").strip()
    text = str(ctx.input.get("text") or "").strip()
    if not text:
        raise PermanentError("what should the message say?")
    url = whatsapp_url(text, str(ctx.input.get("phone") or ""))
    direct = "phone=" in url

    def go():
        if ctx.dry_run:
            return {"would_open": url, "dry_run": True}
        if not WIN:
            raise PermanentError("WhatsApp messages work on Windows only")
        try:
            os.startfile(url)  # type: ignore[attr-defined]  # WhatsApp (desktop or Store app) takes whatsapp://
        except OSError:
            raise PermanentError("WhatsApp isn't installed on this PC (Microsoft Store: WhatsApp)") from None
        return {"ready": True, "to": to, "text": text,
                "next": "the message is typed in their chat: press Enter to send" if direct else
                        f"pick {to or 'the chat'} in WhatsApp, then press Enter to send"}

    return ctx.step("open", go)


@workflow(PLUGIN, "close_app")
def close_app(ctx: Context):
    asked = str(ctx.input.get("name") or "").strip()
    if not asked:
        raise PermanentError("which app?")
    want = _norm(ALIASES.get(_norm(asked), asked))

    def go():
        if not WIN:
            raise PermanentError("closing apps works on Windows only")
        out = _ps("Get-Process | Where-Object { $_.MainWindowHandle -ne 0 } | "
                  "Select-Object Id, ProcessName, MainWindowTitle, @{n='Desc';e={$_.Description}} | "
                  "ConvertTo-Json -Compress")
        rows = json.loads(out or "[]")
        rows = rows if isinstance(rows, list) else [rows]
        hits = [r for r in rows if want in _norm(f"{r.get('ProcessName', '')} {r.get('Desc') or ''} "
                                                  f"{r.get('MainWindowTitle') or ''}")]
        if not hits:
            return {"closed": [], "problem": f"{asked} doesn't seem to be open"}
        if ctx.dry_run:
            return {"would_close": [h.get("MainWindowTitle") or h["ProcessName"] for h in hits], "dry_run": True}
        ids = ",".join(str(int(h["Id"])) for h in hits)
        _ps(f"foreach ($p in Get-Process -Id {ids} -ErrorAction SilentlyContinue) {{ [void]$p.CloseMainWindow() }}")
        return {"closed": [h.get("MainWindowTitle") or h["ProcessName"] for h in hits]}

    return ctx.step("close", go)
