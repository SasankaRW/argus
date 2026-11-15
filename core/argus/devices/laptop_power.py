"""The laptop's own power buttons (Argus's server): restart Argus, restart the laptop, shut it down, cancel.

argusd runs as the `argus` user with NoNewPrivileges, so no sudo: these go through systemd and logind, and a polkit
rule (`deploy/linux/power-setup.sh`, run once) allows exactly these to the argus user and nothing else:
restarting the argusd / argus-worker services, and rebooting or powering off. Reboot and shut down are scheduled a
minute ahead (`shutdown +1`), so Cancel can still stop them; Argus says so on the phone first.

Shut down needs `confirm`: with no screen and no battery, only the laptop's power button brings it back.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEDULED = Path("/run/systemd/shutdown/scheduled")  # logind writes the pending reboot / power-off here
ACTIONS = {
    "restart-argus": ["/usr/bin/systemctl", "restart", "argusd", "argus-worker"],
    "reboot": ["/usr/bin/shutdown", "-r", "+1", "Argus: restart from Helios (cancel there)"],
    "shutdown": ["/usr/bin/shutdown", "-h", "+1", "Argus: shut down from Helios (cancel there)"],
    "cancel": ["/usr/bin/shutdown", "-c"],
}
SETUP = "run once on the laptop: sudo bash /opt/argus/deploy/linux/power-setup.sh"


class LaptopPowerError(Exception):
    pass


def available() -> bool:
    return sys.platform.startswith("linux")


def scheduled(path: Path = SCHEDULED, clock: Callable[[], float] = time.time) -> dict[str, Any] | None:
    """A reboot or power-off waiting to happen: {mode: reboot|poweroff, at}. None when nothing is scheduled."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    kv = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    try:
        at = int(kv.get("USEC", "0")) / 1_000_000
    except ValueError:
        return None
    if at <= clock():
        return None
    mode = kv.get("MODE", "")
    return {"mode": "reboot" if mode == "reboot" else "poweroff", "at": at}


def run(action: str, *, confirm: bool = False, runner: Callable[..., Any] = subprocess.run,
        spawn: Callable[..., Any] = subprocess.Popen) -> dict[str, Any]:
    """Do `action`. restart-argus runs a moment later (this very process is restarted), so the answer gets out."""
    if action not in ACTIONS:
        raise LaptopPowerError(f"no laptop action {action!r}")
    if not available():
        raise LaptopPowerError("the laptop buttons work on the Linux server only")
    if action == "shutdown" and not confirm:
        raise LaptopPowerError("shutting the laptop down needs confirm: only its power button brings it back")
    cmd = ACTIONS[action]
    if action == "restart-argus":
        spawn(["/bin/sh", "-c", "sleep 2; exec " + " ".join(cmd)], start_new_session=True,
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "action": action, "note": "Argus restarts in a few seconds"}
    try:
        p = runner(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        raise LaptopPowerError(f"{cmd[0]}: {e}") from None
    if p.returncode != 0:
        why = (p.stderr or p.stdout or "").strip()[:200]
        if "access denied" in why.lower() or "interactive authentication" in why.lower() or "polkit" in why.lower():
            raise LaptopPowerError(f"not allowed yet ({SETUP})")
        raise LaptopPowerError(f"{action} failed: {why or p.returncode}")
    return {"ok": True, "action": action}
