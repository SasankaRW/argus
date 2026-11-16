"""The Windows send-menu package: scripts parse, and they agree with the manifest and the C++ on names and paths."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MENU = ROOT / "windows" / "send-menu"
SCRIPTS = ROOT / "scripts"
PS = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(PS is None, reason="needs PowerShell")
@pytest.mark.parametrize("path", [SCRIPTS / "send-menu-install.ps1", SCRIPTS / "send-menu-remove.ps1",
                                  MENU / "make-assets.ps1"])
def test_powershell_scripts_parse(path):
    parse = "[System.Management.Automation.Language.Parser]::ParseFile($env:SCRIPT_PATH,[ref]$null,[ref]$e)"
    cmd = ("$e=$null;[void]" + parse + ";"
           "if($e){$e|ForEach-Object{$_.Message};exit 1}")
    r = subprocess.run([PS, "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                       env={**os.environ, "SCRIPT_PATH": str(path)})
    assert r.returncode == 0, r.stdout + r.stderr


def test_manifest_clsids_match_the_cpp_and_both_devices_are_listed():
    manifest = (MENU / "AppxManifest.xml").read_text()
    cpp = (MENU / "send_menu.cpp").read_text()
    ids = set(re.findall(r'Class Id="([0-9a-f-]{36})"', manifest))
    assert len(ids) == 2
    for guid in ids:
        assert guid[:8] in cpp
        assert manifest.count(f'Clsid="{guid}"') == 2          # files and folders
    assert 'Type="*"' in manifest and 'Type="Directory"' in manifest
    assert "Send to my phone" in cpp and "Send to Calypso" in cpp
    assert 'L" send "' in cpp                                  # runs `argus send <device> <paths>`


def test_scripts_use_the_same_identity_and_never_commit_secrets():
    manifest = (MENU / "AppxManifest.xml").read_text()
    publisher = re.search(r'Publisher="([^"]+)"', manifest).group(1)
    name = re.search(r'Identity Name="([^"]+)"', manifest).group(1)
    for script in ("send-menu-install.ps1", "send-menu-remove.ps1"):
        text = (SCRIPTS / script).read_text()
        assert publisher in text and name in text
    install = (SCRIPTS / "send-menu-install.ps1").read_text()
    assert "-ExternalLocation" in install and "LocalMachine\\TrustedPeople" in install and "SendTo" in install
    assert "windows\\send-menu" in install and "ArgusSendMenu.msix" in (MENU / "build.cmd").read_text()
    ignore = (ROOT / ".gitignore").read_text()
    assert "windows/send-menu/build/" in ignore and "*.pfx" in ignore
