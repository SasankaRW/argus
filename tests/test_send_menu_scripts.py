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


def test_each_device_is_its_own_package_with_its_own_clsid_and_one_verb():
    """One verb per package: Windows 11 groups several verbs of a package under a submenu, so no submenu this way."""
    template = (MENU / "AppxManifest.xml.in").read_text()
    cpp = (MENU / "send_menu.cpp").read_text()
    packages = (MENU / "make-packages.ps1").read_text()
    assert template.count("<desktop5:Verb ") == 2 and template.count("<com:Class ") == 1   # files + folders
    assert 'Type="*"' in template and 'Type="Directory"' in template
    assert "ECF_HASSUBCOMMANDS" not in cpp and "ECF_DEFAULT" in cpp
    guids = re.findall(r'Clsid = "([0-9a-f-]{36})"', packages)
    assert len(guids) == 3 and len(set(guids)) == 3
    for guid in guids:   # the C++ struct initialiser must spell the whole GUID (a wrong byte hid Calypso once)
        h = guid.replace("-", "")
        parts = [f"0x{h[:8]}", f"0x{h[8:12]}", f"0x{h[12:16]}"] + [f"0x{h[i:i + 2]}" for i in range(16, 32, 2)]
        assert ", ".join(parts[:3]) + ", {" + ", ".join(parts[3:]) + "}" in cpp
    assert 'L" send "' in cpp                                   # runs `argus send <device> <paths>`
    assert "Send to " in cpp and ".argus-send" in cpp and "devices.json" in cpp   # the title follows the saved name


def test_icons_are_plain_glyphs_in_two_theme_colours():
    glyphs = {line.split("|")[0] for line in (MENU / "glyphs.txt").read_text().splitlines() if line}
    script = (MENU / "make-assets.ps1").read_text()
    cpp = (MENU / "send_menu.cpp").read_text()
    for name in ("smartphone", "laptop_chromebook", "computer"):
        assert name in glyphs and name + "-w500" in glyphs and f'"{name}"' in script
    assert "#ffffff" in script and "#1f1f1f" in script
    assert "FillPath" in script and "#17171c" not in script   # no background squircle
    assert "AppsUseLightTheme" in cpp and "-light.ico" in cpp and "-dark.ico" in cpp


def test_scripts_use_the_same_identity_and_never_commit_secrets():
    template = (MENU / "AppxManifest.xml.in").read_text()
    publisher = re.search(r'Publisher="([^"]+)"', template).group(1)
    for script in ("send-menu-install.ps1", "send-menu-remove.ps1"):
        text = (SCRIPTS / script).read_text()
        for pkg in ("Argus.SendMenu", "Argus.SendPhone", "Argus.SendCalypso"):   # the old one is cleaned up too
            assert pkg in text
    assert publisher in (SCRIPTS / "send-menu-install.ps1").read_text()
    assert 'Name="Argus.Send@ID@"' in template
    install = (SCRIPTS / "send-menu-install.ps1").read_text()
    assert "-ExternalLocation" in install and "SendTo" in install
    assert "LocalMachine" + chr(92) + "TrustedPeople" in install
    assert "send-menu" in install and "ArgusSend-" in install
    assert "ArgusSend-" in (MENU / "make-packages.ps1").read_text()
    ignore = (ROOT / ".gitignore").read_text()
    assert "windows/send-menu/build/" in ignore and "*.pfx" in ignore
