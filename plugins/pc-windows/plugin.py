"""PC windows: switch to an open window, show the desktop, lock the PC, take a screenshot (Windows, your session).

The screenshot lands in Pictures/Screenshots with the default "Screenshot <date> <time>.png" name, so
screenshot-renamer gives it a proper name like any other.
"""

from __future__ import annotations

import io
import json
import os
import platform
import re
import subprocess
import time

from argus.worker import Context, PermanentError, workflow

PLUGIN = "pc-windows"
WIN = platform.system() == "Windows"


def _ps(script: str, timeout: float = 20) -> str:
    if not WIN:
        raise PermanentError("this works on Windows only")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PermanentError(f"PowerShell: {e}") from None
    if p.returncode != 0:
        raise PermanentError((p.stderr or p.stdout).strip()[:300] or "PowerShell failed")
    return p.stdout


def windows() -> list[dict]:
    out = _ps("Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle } | "
              "Select-Object Id, ProcessName, MainWindowTitle | ConvertTo-Json -Compress")
    rows = json.loads(out or "[]")
    return rows if isinstance(rows, list) else [rows]


def pick(asked: str, rows: list[dict]) -> dict | None:
    want = re.sub(r"[^a-z0-9 ]+", " ", asked.lower()).strip()
    alias = {"vs code": "code", "vscode": "code", "chrome": "chrome", "explorer": "explorer", "files": "explorer"}
    want = alias.get(want, want)
    for r in rows:
        if r["ProcessName"].lower() == want:
            return r
    for r in rows:
        hay = f"{r['ProcessName']} {r['MainWindowTitle']}".lower()
        if all(w in hay for w in want.split()):
            return r
    return None


@workflow(PLUGIN, "switch")
def switch(ctx: Context):
    asked = str(ctx.input.get("name") or "").strip()
    if not asked:
        raise PermanentError("which window?")

    def go():
        w = pick(asked, windows())
        if w is None:
            return {"switched": None, "problem": f"{asked} isn't open"}
        if ctx.dry_run:
            return {"would_switch": w["MainWindowTitle"], "dry_run": True}
        _ps("Add-Type @'\nusing System; using System.Runtime.InteropServices;\npublic class W { "
            "[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h); "
            "[DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int c); }\n'@\n"
            f"$h = (Get-Process -Id {int(w['Id'])}).MainWindowHandle; [W]::ShowWindow($h, 9) | Out-Null; "
            "[W]::SetForegroundWindow($h) | Out-Null")
        return {"switched": w["MainWindowTitle"]}

    return ctx.step("switch", go)


@workflow(PLUGIN, "desktop")
def desktop(ctx: Context):
    def go():
        if ctx.dry_run:
            return {"would": "show the desktop", "dry_run": True}
        _ps("(New-Object -ComObject Shell.Application).MinimizeAll()")
        return {"done": "showing the desktop"}

    return ctx.step("desktop", go)


@workflow(PLUGIN, "lock")
def lock(ctx: Context):
    def go():
        if ctx.dry_run:
            return {"would": "lock the PC", "dry_run": True}
        if not WIN:
            raise PermanentError("this works on Windows only")
        subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], timeout=10)
        return {"done": "locked"}

    return ctx.step("lock", go)


@workflow(PLUGIN, "screenshot")
def screenshot(ctx: Context):
    def go():
        name = time.strftime("Screenshot %Y-%m-%d %H%M%S.png")
        folder = os.path.expanduser("~/Pictures/Screenshots")
        if ctx.dry_run:
            return {"would_save": os.path.join(folder, name), "dry_run": True}
        try:
            from PIL import ImageGrab  # type: ignore[import-not-found]
        except ImportError:
            raise PermanentError("needs Pillow (pip install -e .[plugins])") from None
        img = ImageGrab.grab(all_screens=True)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {"saved": ctx.files.write_bytes(os.path.join(folder, name), buf.getvalue())}

    return ctx.step("grab", go)
