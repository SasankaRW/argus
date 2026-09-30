"""Home lab: disks, GPU, Ollama models, Argus backups and Tailscale devices, every hour and on request.

Each part is read on its own; one that can't be read (no nvidia-smi, Tailscale not installed) is left out, never an
error. Warnings are sent once a day each, as ordinary (held) messages, so they land in the evening summary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any

from argus.worker import Context, workflow

PLUGIN = "homelab"


def _run(args: list[str], timeout: float = 10) -> str | None:
    exe = shutil.which(args[0])
    if not exe:
        return None
    try:
        r = subprocess.run([exe, *args[1:]], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def disks() -> list[dict] | None:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    out = []
    for p in psutil.disk_partitions(all=False):
        if "cdrom" in p.opts or not p.fstype:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        out.append({"drive": p.mountpoint, "free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1),
                    "free_percent": round(100 - u.percent, 1)})
    return out


def parse_gpu(text: str | None) -> list[dict] | None:
    if not text:
        return None
    rows = []
    for ln in text.strip().splitlines():
        p = [x.strip() for x in ln.split(",")]
        if len(p) != 5:
            continue
        try:
            rows.append({"name": p[0], "busy_percent": float(p[1]), "memory_used_gb": round(float(p[2]) / 1024, 1),
                         "memory_total_gb": round(float(p[3]) / 1024, 1), "temperature_c": float(p[4])})
        except ValueError:
            continue
    return rows or None


def parse_tailscale(text: str | None) -> dict | None:
    """`tailscale status --json` -> this device and the others, online or not."""
    if not text:
        return None
    try:
        st = json.loads(text)
    except ValueError:
        return None
    peers = [{"name": p.get("HostName") or p.get("DNSName", "?").split(".")[0], "online": bool(p.get("Online")),
              "os": p.get("OS")} for p in (st.get("Peer") or {}).values()]
    peers.sort(key=lambda p: (not p["online"], p["name"].lower()))
    me = st.get("Self") or {}
    return {"this_device": me.get("HostName"), "running": st.get("BackendState") == "Running",
            "online": [p["name"] for p in peers if p["online"]],
            "offline": [p["name"] for p in peers if not p["online"]]}


def warnings(r: dict, cfg: dict) -> list[tuple[str, str]]:
    """(key, text) for each problem; the key is what makes it once a day."""
    out = []
    for d in r.get("disks") or []:
        if d["total_gb"] > 1 and d["free_percent"] < float(cfg.get("disk_free_percent") or 10):
            out.append((f"disk:{d['drive']}", f"{d['drive']} is nearly full: {d['free_gb']} GB free "
                                              f"({d['free_percent']}%)"))
    for g in r.get("gpu") or []:
        if g["temperature_c"] > float(cfg.get("gpu_hot_c") or 85):
            out.append(("gpu:hot", f"the GPU is hot: {g['temperature_c']:.0f} °C"))
    b = r.get("backup")
    if b and b.get("enabled"):
        limit = float(cfg.get("backup_hours") or 48)
        if b.get("hours_ago") is None:
            out.append(("backup:none", "Argus has no backup yet"))
        elif b["hours_ago"] > limit:
            out.append(("backup:old", f"the newest Argus backup is {b['hours_ago']:.0f} hours old"))
    ts = r.get("tailscale")
    if ts is not None and not ts["running"]:
        out.append(("tailscale:down", "Tailscale isn't running on the PC (the phone can't reach Argus)"))
    return out


def look(ctx: Context) -> dict[str, Any]:
    r: dict[str, Any] = {"at": time.strftime("%Y-%m-%d %H:%M")}
    r["disks"] = disks()
    r["gpu"] = parse_gpu(_run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
                                "temperature.gpu", "--format=csv,noheader,nounits"]))
    try:
        tags = ctx.http.get_json(str(ctx.config.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
                                 + "/api/tags")
        r["ollama_models"] = sorted(m.get("name", "?") for m in tags.get("models", []))
    except Exception:  # Ollama off: left out
        r["ollama_models"] = None
    try:
        r["backup"] = ctx.tool("backup_status")
    except Exception:
        r["backup"] = None
    r["tailscale"] = parse_tailscale(_run(["tailscale", "status", "--json"]))
    r["attention"] = [t for _, t in warnings(r, ctx.config)]
    return r


@workflow(PLUGIN, "status")
def status(ctx: Context):
    return ctx.step("look", lambda: look(ctx))


@workflow(PLUGIN, "check")
def check(ctx: Context):
    r = ctx.step("look", lambda: look(ctx))
    today = time.strftime("%Y-%m-%d")

    def tell():
        warned = (ctx.store.get("warned") if ctx.store else None) or {}
        fresh = [(k, t) for k, t in warnings(r, ctx.config) if warned.get(k) != today]
        if fresh and not ctx.dry_run:
            ctx.notify("Home lab: " + ("1 thing" if len(fresh) == 1 else f"{len(fresh)} things") + " to look at",
                       "\n".join(f"- {t}" for _, t in fresh))  # ordinary priority: held for the evening summary
            warned.update({k: today for k, _ in fresh})
            ctx.store.set("warned", {k: v for k, v in warned.items() if v == today})
        return {"new_warnings": [t for _, t in fresh]}

    told = ctx.step("tell", tell)
    return {**r, **told}
