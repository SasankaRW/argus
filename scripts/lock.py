"""The package versions every install uses: constraints.txt, one file for Windows and Linux, Python 3.11 and up.

    python scripts/lock.py             after changing dependencies in pyproject.toml: add what's new, keep the rest
    python scripts/lock.py --upgrade   move everything to the newest versions (then run the tests)
    python scripts/lock.py --check     which packages have newer versions; changes nothing (the monthly check)

CI, the laptop's updater and install script, and dev.ps1 all install with `-c constraints.txt`, so the PC, the
laptop and a fresh install get the versions CI tested. Needs uv (`pip install uv`, or the laptop's own).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "constraints.txt"
EXTRAS = ("dev", "plugins", "console")  # what CI and the laptop install; the PC's voice extras are left free
HEADER = (
    "# The package versions Argus installs with (pip install ... -c constraints.txt). Made by scripts/lock.py:\n"
    "# don't edit by hand. Windows and Linux, Python 3.11 and up.\n"
)
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)\s*(;.*)?$")


def uv() -> list[str]:
    exe = shutil.which("uv")
    if exe:
        return [exe]
    try:
        import uv as uv_mod  # pip install uv

        return [uv_mod.find_uv_bin()]
    except ImportError:
        raise SystemExit("uv is needed: pip install uv") from None


def compile_to(out: Path, upgrade: bool) -> None:
    cmd = [*uv(), "pip", "compile", str(ROOT / "pyproject.toml"), "--universal", "--python-version", "3.11",
           "--no-header", "--quiet", "-o", str(out)]
    for extra in EXTRAS:
        cmd += ["--extra", extra]
    if upgrade:
        cmd.append("--upgrade")
    subprocess.run(cmd, cwd=ROOT, check=True)


def pins(text: str) -> dict[tuple[str, str], str]:
    """{(package, marker): version} from a constraints file."""
    out = {}
    for line in text.splitlines():
        m = PIN.match(line.strip())
        if m:
            out[(m.group(1).lower(), (m.group(3) or "").strip())] = m.group(2)
    return out


def changes(old: str, new: str) -> list[str]:
    a, b = pins(old), pins(new)
    out = []
    for key in sorted(a.keys() | b.keys()):
        name = key[0] + (f" ({key[1].lstrip('; ')})" if key[1] else "")
        if key not in b:
            out.append(f"{name}: {a[key]} -> gone")
        elif key not in a:
            out.append(f"{name}: new, {b[key]}")
        elif a[key] != b[key]:
            out.append(f"{name}: {a[key]} -> {b[key]}")
    return out


def with_header(text: str) -> str:
    body = "".join(line + "\n" for line in text.splitlines() if not line.startswith("# The package versions")
                   and not line.startswith("# don't edit by hand"))
    return HEADER + body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lock", description="Argus's package versions (constraints.txt)")
    ap.add_argument("--upgrade", action="store_true", help="move every package to its newest version")
    ap.add_argument("--check", action="store_true", help="list newer versions, change nothing")
    args = ap.parse_args(argv)
    old = LOCK.read_text(encoding="utf-8") if LOCK.exists() else ""
    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "constraints.txt"
            out.write_text(old, encoding="utf-8")
            compile_to(out, upgrade=True)
            found = changes(old, out.read_text(encoding="utf-8"))
        print("\n".join(found) if found else "Everything is on its newest version.")
        if found:
            print(f"\n{len(found)} to update: python scripts/lock.py --upgrade, run the tests, send it as a change.")
        return 0
    compile_to(LOCK, upgrade=args.upgrade)
    new = with_header(LOCK.read_text(encoding="utf-8"))
    LOCK.write_text(new, encoding="utf-8", newline="\n")
    found = changes(old, new)
    print("\n".join(found) if found else "No version changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
