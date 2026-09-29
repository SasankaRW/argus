"""Power buttons, run by the PC's worker: sleep, shut down, restart, and cancel a pending shutdown.

Jobs of the built-in plugin "power" (queued by argusd from Helios: Power page or phone). Shut down and restart
wait `power.shutdown_delay_seconds` (Windows shows its own warning), so "cancel" can still stop them.
"""

from __future__ import annotations

import platform
import subprocess

from .workflows import Context, PermanentError, workflow

PLUGIN = "power"
WIN = platform.system() == "Windows"
NEEDS = ("desktop",)  # set on the job by argusd: only the PC runs these

# Windows: Application.SetSuspendState sleeps (rundll32 SetSuspendState hibernates when hibernation is on).
SLEEP_PS = ("Add-Type -AssemblyName System.Windows.Forms; "
            "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false) | Out-Null")


def _run(cmd: list[str]) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PermanentError(f"{cmd[0]}: {e}") from None
    if p.returncode != 0:
        raise PermanentError(f"{' '.join(cmd[:2])} failed: {(p.stderr or p.stdout).strip()[:200]}")
    return (p.stdout or "").strip()


def _delay(ctx: Context) -> int:
    return max(0, min(3600, int(ctx.input.get("delay", 60))))


@workflow(PLUGIN, "sleep")
def sleep(ctx: Context):
    def go():
        if WIN:
            subprocess.Popen(["powershell", "-NoProfile", "-Command", SLEEP_PS])  # returns before the PC sleeps
        else:
            _run(["systemctl", "suspend"])
        return "sleeping"

    return {"pc": ctx.step("sleep", go)}


@workflow(PLUGIN, "shutdown")
def shutdown(ctx: Context):
    d = _delay(ctx)

    def go():
        if WIN:
            _run(["shutdown", "/s", "/t", str(d), "/c", "Argus: shutting down (cancel in Helios)"])
        else:
            _run(["shutdown", "-h", f"+{max(1, round(d / 60))}"])
        return f"shutting down in {d} s"

    return {"pc": ctx.step("shutdown", go), "delay": d}


@workflow(PLUGIN, "restart")
def restart(ctx: Context):
    d = _delay(ctx)

    def go():
        if WIN:
            _run(["shutdown", "/r", "/t", str(d), "/c", "Argus: restarting (cancel in Helios)"])
        else:
            _run(["shutdown", "-r", f"+{max(1, round(d / 60))}"])
        return f"restarting in {d} s"

    return {"pc": ctx.step("restart", go), "delay": d}


@workflow(PLUGIN, "cancel")
def cancel(ctx: Context):
    def go():
        try:
            _run(["shutdown", "/a"] if WIN else ["shutdown", "-c"])
            return "cancelled"
        except PermanentError as e:  # nothing pending: fine
            return f"nothing to cancel ({e})"

    return {"pc": ctx.step("cancel", go)}


def input_idle_seconds() -> float | None:
    """How long nobody touched the keyboard or mouse (Windows), or None when unknown."""
    if not WIN:
        return None
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):  # type: ignore[attr-defined]
        return None
    return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0  # type: ignore[attr-defined]


@workflow(PLUGIN, "auto_shutdown")
def auto_shutdown(ctx: Context):
    """The power manager's shutdown after an idle stretch: never while someone uses the PC."""
    need = float(ctx.input.get("idle_minutes", 20)) * 60

    def go():
        idle = input_idle_seconds()
        if idle is not None and idle < need:
            return {"skipped": f"someone used the PC {round(idle / 60)} min ago"}
        d = _delay(ctx)
        if WIN:
            _run(["shutdown", "/s", "/t", str(d), "/c", "Argus: idle, shutting down (cancel in Helios)"])
        else:
            _run(["shutdown", "-h", f"+{max(1, round(d / 60))}"])
        return {"pc": f"shutting down in {d} s", "input_idle_seconds": idle}

    return ctx.step("shutdown", go)
