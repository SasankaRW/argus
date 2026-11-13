"""The code's status page, read from the code itself, so it can't go out of date.

    python scripts/status.py                 print it (Markdown)
    python scripts/status.py --write         write docs/status.md
    python scripts/status.py --no-tests      skip counting the tests (counting takes a few seconds)

The release tool writes docs/status.md with every release. Nothing here is typed by hand.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BIG = 900  # lines: a file this long is listed as big


def version(root: Path = ROOT) -> str:
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def schema(root: Path = ROOT) -> int:
    nums = [int(m.group(1)) for f in (root / "core/argus/db/migrations").glob("*.sql")
            if (m := re.match(r"(\d{4})_", f.name))]
    return max(nums, default=0)


def plugins(root: Path = ROOT) -> list[dict]:
    out = []
    for manifest in sorted((root / "plugins").glob("*/plugin.yaml")):
        m = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        out.append({"id": m.get("id", manifest.parent.name), "name": m.get("name", ""),
                    "runs_on": m.get("runs_on", "any"), "workflows": len(m.get("workflows") or [])})
    return out


def routes(root: Path = ROOT) -> int | None:
    """API routes of a real app (built against a throwaway config, never the real database)."""
    sys.path.insert(0, str(root / "core"))
    try:
        from argus.api import create_app
        from argus.config import load_config
        from argus.context import Argus

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "argus.yaml").write_text("logging:\n  file: null\n", encoding="utf-8")
            app = create_app(Argus(load_config(Path(tmp) / "argus.yaml")))
            return sum(1 for r in app.routes if getattr(r, "path", "").startswith("/")
                       and not r.path.startswith(("/docs", "/redoc", "/openapi")))
    except Exception as e:  # a status page should still come out
        print(f"warning: could not build the app to count routes: {e}", file=sys.stderr)
        return None


def tests(root: Path = ROOT) -> int | None:
    p = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                       cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"(\d+) tests? collected", p.stdout) or re.search(r"^(\d+) tests?\b", p.stdout, re.M)
    return int(m.group(1)) if m else None


def lines(paths: list[Path]) -> int:
    return sum(len(p.read_text(encoding="utf-8", errors="replace").splitlines()) for p in paths)


def code(root: Path = ROOT) -> dict:
    py = {part: sorted(p for p in (root / part).rglob("*.py") if "__pycache__" not in p.parts)
          for part in ("core", "plugins", "tests", "scripts")}
    helios = sorted(p for ext in ("*.ts", "*.tsx") for p in (root / "helios/src").rglob(ext))
    sizes = [(len(p.read_text(encoding="utf-8", errors="replace").splitlines()), p.relative_to(root).as_posix())
             for part in ("core", "plugins") for p in py[part]]
    core_flat = [p for p in (root / "core/argus").glob("*.py")]
    return {"python": {k: lines(v) for k, v in py.items()}, "helios": lines(helios),
            "helios_tests": sum(1 for p in (root / "helios").rglob("*.test.*") if "node_modules" not in p.parts),
            "big": sorted((s for s in sizes if s[0] >= BIG), reverse=True), "core_flat": len(core_flat)}


def commit(root: Path = ROOT) -> str:
    p = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True)
    return p.stdout.strip() or "unknown"


def render(root: Path = ROOT, count_tests: bool = True) -> str:
    plug, c = plugins(root), code(root)
    n_tests, n_routes = (tests(root) if count_tests else None), routes(root)
    py = c["python"]
    rows = [
        ("Version", version(root)),
        ("Database schema", f"v{schema(root)}"),
        ("Plugins", str(len(plug))),
        ("Tests", "not counted" if n_tests is None else str(n_tests)),
        ("API routes", "unknown" if n_routes is None else str(n_routes)),
        ("Python lines", f"{sum(py.values()):,} (core {py['core']:,}, plugins {py['plugins']:,}, "
                         f"tests {py['tests']:,}, scripts {py['scripts']:,})"),
        ("Modules directly in core/argus", str(c["core_flat"])),
        ("Helios lines", f"{c['helios']:,} ({c['helios_tests']} test files)"),
    ]
    out = ["# Argus code status", "",
           f"Made by `scripts/status.py` from commit `{commit(root)}`. Don't edit by hand: run "
           "`python scripts/status.py --write` (the release tool does it for every release).", "",
           "| | |", "| --- | --- |", *[f"| {k} | {v} |" for k, v in rows], "",
           f"## Files over {BIG} lines", "", "| Lines | File |", "| ---: | --- |",
           *[f"| {n:,} | `{f}` |" for n, f in c["big"]], "",
           "## Plugins", "", "| Plugin | Name | Runs on | Workflows |", "| --- | --- | --- | ---: |",
           *[f"| `{p['id']}` | {p['name']} | {p['runs_on']} | {p['workflows']} |" for p in plug], ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="status", description="Argus code status, read from the code")
    ap.add_argument("--write", action="store_true", help="write docs/status.md instead of printing")
    ap.add_argument("--no-tests", action="store_true", help="don't count the tests")
    args = ap.parse_args(argv)
    page = render(count_tests=not args.no_tests)
    if args.write:
        (ROOT / "docs/status.md").write_text(page, encoding="utf-8", newline="\n")
        print("wrote docs/status.md")
    else:
        sys.stdout.write(page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
