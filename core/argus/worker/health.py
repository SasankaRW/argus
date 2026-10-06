"""The PC's health: WSL and Docker (job health.check, every `health.every_minutes` while the PC is on).

Returns what it saw; the phone hears only about changes (a container you listed stopped, a container reports
unhealthy, Docker stopped answering), not the same problem every half hour. The last state is kept in
data/health-last.json on the PC.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path

from .workflows import Context, workflow

LAST = Path("data") / "health-last.json"


def _run(cmd: list[str], timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    raw = p.stdout or p.stderr
    text = raw.decode("utf-16-le", "replace") if raw[:2] in (b"\xff\xfe",) or b"\x00" in raw[:40] else \
        raw.decode("utf-8", "replace")  # wsl.exe answers in UTF-16
    return p.returncode, text.replace("\x00", "")


def wsl() -> list[dict] | None:
    """WSL distributions and whether they run (Windows only)."""
    if platform.system() != "Windows" or not shutil.which("wsl"):
        return None
    code, out = _run(["wsl", "-l", "-v"])
    if code != 0:
        return None
    rows = []
    for ln in out.splitlines()[1:]:
        parts = ln.replace("*", " ").split()
        if len(parts) >= 3:
            rows.append({"name": parts[0], "state": parts[1], "version": parts[2]})
    return rows


def docker() -> dict | None:
    """Containers and their state; None when Docker isn't installed; {"up": False} when it doesn't answer."""
    if not shutil.which("docker"):
        return None
    code, out = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
    if code != 0:
        return {"up": False, "error": out.strip()[:200]}
    items = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 3:
            items.append({"name": parts[0], "state": parts[1], "status": parts[2]})
    return {"up": True, "containers": items, "running": sum(1 for c in items if c["state"] == "running")}


def problems(d: dict | None, expected: list[str]) -> list[str]:
    out: list[str] = []
    if d is None:
        return ["Docker is not installed"] if expected else []
    if not d.get("up"):
        return ["Docker is not answering (Docker Desktop stopped?)"]
    by = {c["name"]: c for c in d["containers"]}
    for name in expected:
        c = by.get(name)
        if c is None:
            out.append(f"{name} is missing")
        elif c["state"] != "running":
            out.append(f"{name} is {c['state']}")
    out += [f"{c['name']} is unhealthy" for c in d["containers"] if "(unhealthy)" in c["status"]]
    return out


@workflow("health", "check")
def check(ctx: Context):
    expected = [str(x) for x in ctx.input.get("containers") or []]

    def look():
        d = docker()
        return {"wsl": wsl(), "docker": d, "problems": problems(d, expected)}

    seen = ctx.step("look", look)
    try:
        before = json.loads(LAST.read_text())
    except (OSError, ValueError):
        before = {"problems": []}
    new = [p for p in seen["problems"] if p not in before.get("problems", [])]
    fixed = [p for p in before.get("problems", []) if p not in seen["problems"]]
    if new or fixed:
        def tell():
            text = "\n".join([*(f"Problem: {p}" for p in new), *(f"OK again: {p}" for p in fixed)])
            ctx.notify("PC health: " + ("something stopped" if new else "all fine again"), text,
                       priority="high" if new else "default", tags=["warning" if new else "white_check_mark"])
            return True
        ctx.step("tell", tell)
    try:
        LAST.parent.mkdir(parents=True, exist_ok=True)
        LAST.write_text(json.dumps({"problems": seen["problems"]}))
    except OSError:
        pass
    return seen


@workflow("ari", "health")
def ari_health(ctx: Context):
    """The PC's part of Ari's health check (Argus on the laptop asks for it): Ollama, SearXNG, the voice and Whisper
    as this PC sees them, with this PC's own argus.yaml. Nothing here changes anything."""
    from .. import health
    from ..config import Config, ConfigError, load_config

    try:
        cfg = load_config()
    except ConfigError:
        cfg = Config()
    return {"checks": health.pc_part(cfg, list(ctx.input.get("tools") or []))}
