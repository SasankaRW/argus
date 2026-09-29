"""Release Argus in one command.

    python scripts/release.py minor            (or: .\\scripts\\dev.ps1 release minor)
    python scripts/release.py patch --dry-run  show what would happen, change nothing

Steps: check you are on an up-to-date, clean main -> lint + tests -> bump VERSION -> turn the changelog's
"Unreleased" section into the new version -> commit "Release vX.Y.Z" -> annotated tag -> push main and the
tag -> GitHub release with the same notes (if the GitHub CLI is installed).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARTS = ("patch", "minor", "major")
UNRELEASED = "## Unreleased"


class ReleaseError(Exception):
    pass


# ---------------------------------------------------------------------- pure helpers (tested)


def bump(version: str, part: str) -> str:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip())
    if not m:
        raise ReleaseError(f"VERSION is not MAJOR.MINOR.PATCH: {version!r}")
    major, minor, patch = map(int, m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"part must be one of {PARTS}")


def unreleased_notes(changelog: str) -> str:
    """The text under '## Unreleased' up to the next '## ' heading."""
    lines = changelog.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == UNRELEASED)
    except StopIteration:
        raise ReleaseError("CHANGELOG.md has no '## Unreleased' section") from None
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def apply_release(changelog: str, version: str, date: str) -> str:
    """Rename 'Unreleased' to the version and put a fresh empty 'Unreleased' above it."""
    if not unreleased_notes(changelog):
        raise ReleaseError("Nothing under '## Unreleased' in CHANGELOG.md: add what changed first")
    if re.search(rf"^## {re.escape(version)}\b", changelog, flags=re.M):
        raise ReleaseError(f"CHANGELOG.md already has an entry for {version}")
    return changelog.replace(UNRELEASED, f"{UNRELEASED}\n\n## {version} ({date})", 1)


# ---------------------------------------------------------------------- git and friends


def run(*cmd: str, check: bool = True, env: dict | None = None, capture: bool = True) -> str:
    r = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture, env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip() if capture else ""
        raise ReleaseError(f"`{' '.join(cmd)}` failed{': ' + detail if detail else ''}")
    return (r.stdout or "").strip() if capture else ""


def has_remote() -> bool:
    return "origin" in run("git", "remote").split()


def preflight() -> None:
    branch = run("git", "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise ReleaseError(f"Releases are made from main (you are on {branch}). Run: .\\scripts\\dev.ps1 sync")
    if run("git", "status", "--porcelain"):
        raise ReleaseError("You have uncommitted changes. Commit or stash them first.")
    if has_remote():
        run("git", "fetch", "--quiet", "origin", "main", "--tags")
        behind = run("git", "rev-list", "--count", "HEAD..origin/main")
        if behind != "0":
            raise ReleaseError(f"main is {behind} commit(s) behind GitHub. Run: .\\scripts\\dev.ps1 sync")


def checks() -> None:
    print("Running lint and tests ...")
    run(sys.executable, "-m", "ruff", "check", "core", "tests", "scripts", capture=False)
    run(sys.executable, "-m", "pytest", "-q", capture=False)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="release", description="Release Argus")
    p.add_argument("part", choices=PARTS)
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("--skip-tests", action="store_true")
    p.add_argument("--no-push", action="store_true", help="commit and tag locally only")
    args = p.parse_args(argv)

    try:
        version_file, changelog_file = ROOT / "VERSION", ROOT / "CHANGELOG.md"
        current = version_file.read_text(encoding="utf-8").strip()
        new = bump(current, args.part)
        changelog = changelog_file.read_text(encoding="utf-8")
        notes = unreleased_notes(changelog)
        updated = apply_release(changelog, new, dt.date.today().isoformat())
        tag = f"v{new}"

        print(f"Argus {current} -> {new} ({args.part})\n\nRelease notes:\n{notes}\n")
        if args.dry_run:
            print("Dry run: nothing changed.")
            return 0

        preflight()
        if run("git", "tag", "--list", tag):
            raise ReleaseError(f"Tag {tag} already exists")
        if not args.skip_tests:
            checks()

        version_file.write_text(new + "\n", encoding="utf-8")
        changelog_file.write_text(updated, encoding="utf-8")
        run("git", "add", "VERSION", "CHANGELOG.md")
        run("git", "commit", "-q", "-m", f"Release {tag}", "-m", notes)
        run("git", "tag", "-a", tag, "-m", f"Argus {new}\n\n{notes}")
        print(f"Committed and tagged {tag}.")

        if args.no_push or not has_remote():
            print("Not pushed (no GitHub remote yet, or --no-push).")
            return 0
        try:
            run("git", "push", "origin", "main", env={"ARGUS_RELEASE": "1"})
            run("git", "push", "origin", tag)
        except ReleaseError as e:
            raise ReleaseError(
                f"{e}\n  The release commit and tag exist here but are not on GitHub. When the network is back:\n"
                f"    $env:ARGUS_RELEASE=1; git push origin main; git push origin {tag}\n"
                f"  or undo it:  git tag -d {tag}; git reset --hard HEAD~1") from None
        print("Pushed main and the tag.")
        if shutil.which("gh"):
            r = subprocess.run(["gh", "release", "create", tag, "--title", f"Argus {new}", "--notes-file", "-"],
                               cwd=ROOT, input=notes, text=True, capture_output=True, encoding="utf-8",
                               errors="replace")
            print("GitHub release created." if r.returncode == 0 else f"GitHub release skipped: {r.stderr.strip()}")
        print(f"\nDone: Argus {new}.")
        return 0
    except ReleaseError as e:
        print(f"Release stopped: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
