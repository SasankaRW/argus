"""PC status: CPU, memory, disks, GPU (nvidia-smi) and the busiest programs; the clipboard (read; write asks first).
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import time

from argus.worker import Context, PermanentError, workflow

PLUGIN = "pc-system"
WIN = platform.system() == "Windows"


def gpu() -> list[dict] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    rows = []
    for ln in out.strip().splitlines():
        p = [x.strip() for x in ln.split(",")]
        if len(p) == 5:
            rows.append({"name": p[0], "busy_percent": _num(p[1]), "memory_used_gb": round(_num(p[2]) / 1024, 1),
                         "memory_total_gb": round(_num(p[3]) / 1024, 1), "temperature_c": _num(p[4])})
    return rows or None


def _num(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return 0.0


def status() -> dict:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        raise PermanentError("needs psutil (pip install -e .[plugins])") from None
    mem = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks.append({"drive": part.mountpoint, "free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1),
                      "used_percent": u.percent})
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            procs.append((p.info["name"] or "?", p.info["memory_info"].rss if p.info["memory_info"] else 0, p))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for _, _, p in procs:
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    cpu = psutil.cpu_percent(interval=1.0)
    by_cpu = []
    for name, _, p in procs:
        try:
            by_cpu.append((name, p.cpu_percent(None) / (psutil.cpu_count() or 1)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    merged: dict[str, list[float]] = {}
    for name, rss, _ in procs:
        merged.setdefault(name, [0.0, 0.0])[1] += rss
    for name, c in by_cpu:
        merged.setdefault(name, [0.0, 0.0])[0] += c
    top_cpu = sorted(merged.items(), key=lambda x: -x[1][0])[:5]
    top_mem = sorted(merged.items(), key=lambda x: -x[1][1])[:5]
    return {"cpu_percent": cpu, "memory_used_gb": round(mem.used / 1e9, 1),
            "memory_total_gb": round(mem.total / 1e9, 1), "memory_percent": mem.percent, "disks": disks, "gpu": gpu(),
            "busiest_cpu": [{"program": n, "cpu_percent": round(v[0], 1)} for n, v in top_cpu if v[0] > 0.5],
            "most_memory": [{"program": n, "memory_gb": round(v[1] / 1e9, 2)} for n, v in top_mem],
            "up_since": time.strftime("%Y-%m-%d %H:%M", time.localtime(psutil.boot_time()))}


@workflow(PLUGIN, "status")
def status_job(ctx: Context):
    return ctx.step("look", status)


def _clip_ps(script: str) -> str:
    if not WIN:
        raise PermanentError("the clipboard works on Windows only")
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script], capture_output=True,
                       text=True, timeout=15, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise PermanentError((p.stderr or p.stdout).strip()[:300])
    return p.stdout


@workflow(PLUGIN, "clipboard_read")
def clipboard_read(ctx: Context):
    def go():
        t = _clip_ps("[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw")
        return {"text": t[:4000], "cut": len(t) > 4000}

    return ctx.step("read", go)


@workflow(PLUGIN, "clipboard_write")
def clipboard_write(ctx: Context):
    text = str(ctx.input.get("text") or "")
    if not text:
        raise PermanentError("nothing to copy")

    def go():
        if ctx.dry_run:
            return {"would_copy": text[:200], "dry_run": True}
        if not WIN:
            raise PermanentError("the clipboard works on Windows only")
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command",
                            "$t = [Console]::In.ReadToEnd(); Set-Clipboard -Value $t"], input=text, text=True,
                           capture_output=True, timeout=15, encoding="utf-8")
        if p.returncode != 0:
            raise PermanentError((p.stderr or p.stdout).strip()[:300])
        return {"copied": len(text)}

    return ctx.step("copy", go)
