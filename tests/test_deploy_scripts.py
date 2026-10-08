"""The laptop install scripts: valid shell, Arch ones use pacman (never apt) and keep the firewall rule."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

LINUX = Path(__file__).resolve().parents[1] / "deploy" / "linux"
SCRIPTS = ["install.sh", "install-arch.sh", "tracker-arch.sh", "argus-update.sh", "auto-update.sh"]


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                    reason="needs a real bash (Windows only has WSL, often with no distro); Linux CI checks these")
@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_parse(name):
    script = (LINUX / name).read_bytes().replace(b"\r\n", b"\n")
    r = subprocess.run(["bash", "-n"], input=script, capture_output=True)
    assert r.returncode == 0, r.stderr.decode(errors="replace")


def test_arch_install_uses_pacman_and_stays_lean():
    text = (LINUX / "install-arch.sh").read_text()
    assert "pacman" in text and "apt-get" not in text and "unattended" not in text   # you update Arch by hand
    assert "-Syu" in text and "pacman -Sy " not in text                              # no partial upgrades
    assert "ufw allow in on tailscale0" in text and "ufw allow 22/tcp" in text      # only Tailscale and SSH
    assert "HandleLidSwitch=ignore" in text
    assert 'cd "$DIR"' in text
    assert "sudo -u argus" not in text          # -H: otherwise uv and pip read the caller's home and fail


def test_tracker_script_binds_to_localhost_only():
    text = (LINUX / "tracker-arch.sh").read_text()
    assert "PORT=127.0.0.1:8282" in text and "--https=8443" in text


def test_the_laptop_updates_itself_and_rolls_back():
    timer = (LINUX / "argus-update.timer").read_text()
    unit = (LINUX / "argus-update.service").read_text()
    assert "OnUnitActiveSec=5min" in timer and "WantedBy=timers.target" in timer
    assert "User=argus" in unit and "ExecStart=/usr/bin/bash /opt/argus/deploy/linux/argus-update.sh" in unit
    assert "NoNewPrivileges=true" not in unit  # it restarts the services through the installer's one sudo rule
    upd = (LINUX / "argus-update.sh").read_text()
    assert "trap rollback ERR" in upd and 'exit 0   # nothing merged' in upd
    setup = (LINUX / "auto-update.sh").read_text()
    assert "deploy-key add" in setup and "enable --now argus-update.timer" in setup and "sudo -u argus" not in setup


def test_the_pc_never_runs_a_second_argus_and_ci_stays_small():
    root = LINUX.parents[1]
    dev = (root / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    assert 'Get-ScheduledTask -TaskName "Argus PC worker"' in dev and "pc-worker.ps1\") restart" in dev
    assert '$env:ARGUS_MERGE_LOCAL -eq "1"' in dev and "--admin" in dev
    import yaml

    ci = yaml.safe_load((root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert "'[\"3.12\"]'" in ci["jobs"]["test"]["strategy"]["matrix"]["python"]  # PRs: one Python per OS
