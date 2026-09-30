"""Is the PC ready to become the laptop's worker? Looks, changes nothing (`pc-worker.ps1 check <laptop url>`).

    python -m argus.movecheck http://laptop:8600

Each line is [ok], [!!] (fix before the move) or [--] (worth a look). The exit code is 1 when something must be
fixed first.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .config import parse_env_file

OK, FIX, LOOK = "ok", "!!", "--"


@dataclass
class Line:
    mark: str
    what: str
    detail: str = ""

    def __str__(self) -> str:
        return f"[{self.mark}] {self.what}" + (f": {self.detail}" if self.detail else "")


Get = Callable[[str, str | None], tuple[int, Any]]


def http_get(url: str, token: str | None = None, timeout: float = 5) -> tuple[int, Any]:
    """(status, json or None); status 0 when it can't be reached."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        body, status = e.read(), e.code
    except (OSError, ValueError):
        return 0, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def check_laptop(url: str, token: str | None, get: Get) -> list[Line]:
    out = []
    status, body = get(url.rstrip("/") + "/version", None)
    if status != 200 or not isinstance(body, dict):
        return [Line(FIX, "laptop", f"can't reach {url} (is argusd running there, and Tailscale up on both?)")]
    theirs = str(body.get("version") or "?")
    out.append(Line(OK, "laptop", f"{url} answers, Argus {theirs}"))
    if theirs != __version__:
        out.append(Line(LOOK, "versions", f"laptop {theirs}, this PC {__version__}: pull the same main on both"))
    if not token:
        out.append(Line(FIX, "worker token", "no ARGUS_WORKER_TOKEN in .env: copy it from the laptop's .env"))
        return out
    status, _ = get(url.rstrip("/") + "/workers", token)
    if status == 200:
        out.append(Line(OK, "worker token", "the laptop accepts it"))
    elif status in (401, 403):
        out.append(Line(FIX, "worker token", "the laptop refuses it: copy ARGUS_WORKER_TOKEN from /opt/argus/.env"))
    else:
        out.append(Line(LOOK, "worker token", f"couldn't check (HTTP {status})"))
    return out


def check_here(get: Get, tiers: list[str], ollama: str) -> list[Line]:
    out = []
    status, _ = get("http://127.0.0.1:8600/version", None)
    if status == 200:
        out.append(Line(FIX, "Argus on this PC", "still running: `.\\scripts\\dev.ps1 down` first (two Argus would "
                                                  "both run your schedules)"))
    else:
        out.append(Line(OK, "Argus on this PC", "stopped"))
    status, body = get(ollama.rstrip("/") + "/api/tags", None)
    if status != 200 or not isinstance(body, dict):
        out.append(Line(FIX, "Ollama", f"not answering at {ollama} (winget install Ollama.Ollama, then ollama serve)"))
    else:
        have = {m.get("name", "") for m in body.get("models") or []}
        have |= {n.split(":")[0] + ":latest" for n in have if ":" not in n}
        missing = [t for t in tiers if t not in have and f"{t}:latest" not in have]
        if missing:
            out.append(Line(FIX, "Ollama models", "missing " + ", ".join(missing) + " (ollama pull <name>)"))
        else:
            out.append(Line(OK, "Ollama models", ", ".join(tiers) or "none needed"))
    return out


def check_windows(run: Callable[[list[str]], str]) -> list[Line]:
    """Fast Startup off (it stops Wake-on-LAN from working) and the wired card's address for power.pc_mac."""
    out = []
    reg = run(["reg", "query", r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power", "/v",
               "HiberbootEnabled"])
    if "0x1" in reg:
        out.append(Line(FIX, "Fast Startup", "on: turn it off (Power Options > Choose what the power buttons do)"))
    elif "0x0" in reg:
        out.append(Line(OK, "Fast Startup", "off"))
    else:
        out.append(Line(LOOK, "Fast Startup", "couldn't tell"))
    macs = []
    for row in run(["getmac", "/fo", "csv", "/nh", "/v"]).splitlines():
        cells = [c.strip('"') for c in row.split('","')]
        if len(cells) >= 3 and "ethernet" in cells[0].lower() and "-" in cells[2]:
            macs.append(f"{cells[1]} {cells[2]}")
    if macs:
        out.append(Line(OK, "wired card", "; ".join(macs) + " (put it in the laptop's power.pc_mac)"))
    else:
        out.append(Line(LOOK, "wired card", "none found: Wake-on-LAN needs the PC on a cable"))
    return out


def check_files(root: Path) -> list[Line]:
    db = root / "data" / "argus.db"
    if not db.exists():
        return [Line(LOOK, "database", f"no {db}: nothing to move")]
    mb = db.stat().st_size / 1e6
    return [Line(OK, "database", f"{db} ({mb:.1f} MB) to copy to the laptop (deploy/README.md, step 2)")]


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="argus-movecheck", description=__doc__.split("\n")[0])
    p.add_argument("url", help="the laptop's Argus, e.g. http://laptop:8600")
    args = p.parse_args(argv)
    root = Path.cwd()
    token = os.environ.get("ARGUS_WORKER_TOKEN") or parse_env_file(root / ".env").get("ARGUS_WORKER_TOKEN")
    tiers: list[str] = []
    ollama = os.environ.get("ARGUS_OLLAMA_URL", "http://127.0.0.1:11434")
    try:
        from .config import load_config

        cfg = load_config(root / "argus.yaml")
        tiers = sorted({t.model for t in cfg.models.tiers.values() if t.provider == "ollama" and t.model})
    except Exception:
        pass
    lines = check_laptop(args.url, token, http_get) + check_here(http_get, tiers, ollama) + check_files(root)
    if os.name == "nt":
        lines += check_windows(_run)
    for line in lines:
        print(line)
    need = sum(1 for x in lines if x.mark == FIX)
    print(f"\n{need} to fix before the move." if need else "\nReady: .\\scripts\\pc-worker.ps1 install " + args.url)
    return 1 if need else 0


if __name__ == "__main__":
    sys.exit(main())
