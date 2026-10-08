"""Change management: turn a change package into a branch with one clear, tested commit.

    python scripts/change.py apply PACKAGE.tgz [--no-tests | --all-tests]   (or: .\\scripts\\dev.ps1 apply PACKAGE.tgz)
    python scripts/change.py check-title "feat(ari): ..."     is this a good commit / PR title?

A change package is a .tgz of changed files plus `.change/change.json`:

    {"branch": "feat/ari-direct-paths", "title": "feat(ari): answer plain status questions without the model",
     "body": "## What\\n...\\n## Why\\n...\\n## How tested\\n...\\n## Risk\\n...",
     "changelog": "- **Faster:** ...", "deleted": ["old/file.py"]}

`apply` refuses a dirty tree, a branch name that exists or was used by any pull request before (GitHub deletes
merged branches, and a reused name lands on the old PR), and a title that doesn't follow the convention. Then:
fresh main -> new branch -> files -> deletions -> changelog line under "Unreleased" -> Helios rebuilt if its
source changed -> lint + the tests for the changed files (`affected_tests`; the full suite runs in CI, or here with
--all-tests or ARGUS_ALL_TESTS=1) -> one commit with the title and body. The body is also kept for `dev.ps1 pr`.
See CONTRIBUTING.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPES = ("feat", "fix", "perf", "refactor", "docs", "test", "chore", "ci", "build")
TITLE = re.compile(r"^(?P<type>" + "|".join(TYPES) + r")(?:\((?P<scope>[a-z0-9][a-z0-9-]*)\))?!?: (?P<what>\S.*)$")
BRANCH = re.compile(r"^(?:" + "|".join(TYPES) + r")/[a-z0-9][a-z0-9.-]{2,48}$")
SECTIONS = ("## What", "## Why", "## How tested", "## Risk")
BANNED = re.compile(r"^(?:co-authored-by:.*claude|claude-session:)", re.I | re.M)
UNRELEASED = "## Unreleased"
META = ".change/change.json"


class ChangeError(Exception):
    pass


# ---------------------------------------------------------------------- pure helpers (tested)


def title_problem(title: str) -> str | None:
    """Why this isn't a good commit/PR title, or None. "type(scope): what changed", imperative, <= 72 chars."""
    t = title.strip()
    if not t:
        return "the title is empty"
    if len(t) > 72:
        return f"the title is {len(t)} characters; keep it to 72"
    m = TITLE.match(t)
    if not m:
        return f"use 'type(scope): what changed' with type one of {', '.join(TYPES)}"
    what = m.group("what")
    if what.endswith("."):
        return "no full stop at the end of the title"
    if what[0].isupper() and not what.split()[0].isupper():
        return "start the summary in lower case (\"add ...\", not \"Add ...\")"
    if what.lower().startswith(("added ", "fixed ", "changed ", "updated ", "removed ")):
        return "write it as a command: \"add ...\", not \"added ...\""
    return None


def branch_problem(name: str) -> str | None:
    if not BRANCH.match(name):
        return f"name the branch type/short-topic in lower case, e.g. feat/ari-direct-paths (type: {', '.join(TYPES)})"
    return None


def body_problem(body: str) -> str | None:
    missing = [s for s in SECTIONS if s not in body]
    if missing:
        return "the description needs these sections: " + ", ".join(missing)
    if BANNED.search(body):
        return "no Claude attribution lines in commits or pull requests"
    return None


def add_changelog(changelog: str, entry: str) -> str:
    """`entry` (one or more '- ' lines) at the top of the 'Unreleased' section."""
    entry = entry.strip()
    if not entry:
        raise ChangeError("the package has no changelog line")
    lines = changelog.splitlines()
    try:
        at = next(i for i, line in enumerate(lines) if line.strip() == UNRELEASED)
    except StopIteration:
        raise ChangeError("CHANGELOG.md has no '## Unreleased' section") from None
    out = lines[:at + 1] + [""] + entry.splitlines() + ([""] if lines[at + 1:at + 2] != [""] else []) + lines[at + 1:]
    return "\n".join(out) + ("\n" if changelog.endswith("\n") else "")


def commit_message(title: str, body: str) -> str:
    return f"{title.strip()}\n\n{body.strip()}\n"


def read_meta(raw: bytes) -> dict:
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ChangeError(f"{META} is not valid JSON: {e}") from None
    for key in ("branch", "title", "body", "changelog"):
        if not str(meta.get(key) or "").strip():
            raise ChangeError(f"{META} has no {key!r}")
    for check in (title_problem(meta["title"]), branch_problem(meta["branch"]), body_problem(meta["body"])):
        if check:
            raise ChangeError(check)
    return meta


# ---------------------------------------------------------------------- git and the rest


def run(*cmd: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise ChangeError(f"{' '.join(cmd)} failed:\n{(p.stderr or p.stdout).strip()[:800]}")
    return p


def git(*args: str, check: bool = True) -> str:
    return run("git", *args, check=check).stdout.strip()


def branch_used(name: str) -> str | None:
    """Where this branch name was used before, or None."""
    if git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False):
        return "a local branch has that name"
    if "origin" in git("remote").split():
        if run("git", "ls-remote", "--exit-code", "--heads", "origin", name, check=False).returncode == 0:
            return "GitHub has a branch with that name"
        if shutil.which("gh"):
            p = run("gh", "pr", "list", "--head", name, "--state", "all", "--json", "number", check=False)
            if p.returncode == 0 and json.loads(p.stdout or "[]"):
                return f"pull request #{json.loads(p.stdout)[0]['number']} already used that name"
    return None


def safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    out = []
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
            raise ChangeError(f"refusing an unsafe path in the package: {m.name}")
        if p.parts and p.parts[0] in (".git", ".change"):
            continue
        if m.name in (".deleted", ".changelog.md"):
            continue
        out.append(m)
    return out


# A change to these runs every test (shared test helpers, packaging, the database schema)
EVERYTHING = re.compile(r"^(?:pyproject\.toml|tests/(?:conftest|fakes|test_worker|_tiny_whisper)\.py|"
                        r"core/argus/db/|core/argus/__init__\.py)")


# The hub modules every running Argus goes through: a change there runs the tests that start one
WIDE = re.compile(r"^core/argus/(?:api/|jobs/|context\.py|config\.py|worker/(?:runner|workflows|client)\.py)")


def _imports(text: str, dotted: str) -> bool:
    """Does this test file use module `argus.x.y` (any import form, or a monkeypatch by name)?"""
    parent, _, stem = dotted.rpartition(".")
    if re.search(rf"\b{re.escape(dotted)}\b", text):
        return True
    for m in re.finditer(rf"from {re.escape(parent)} import (\([^)]*\)|[^\n]*)", text):
        if re.search(rf"\b{re.escape(stem)}\b", m.group(1)):
            return True
    return False


def affected_tests(changed: list[str], root: Path = ROOT) -> list[str] | None:
    """The test files for these changed paths (repo-relative, forward slashes); None: run them all. A test file
    counts when it changed, uses a changed module, or names a changed plugin or script; Helios changes run the
    Helios tests. Indirect effects are left to CI's full run."""
    tests = {p.name: p.read_text(encoding="utf-8", errors="replace")
             for p in sorted((root / "tests").glob("test_*.py"))}
    picked: set[str] = set()
    for rel in changed:
        if EVERYTHING.match(rel):
            return None
        path = Path(rel)
        if rel.startswith("tests/") and path.name in tests:
            picked.add(path.name)
        elif rel.startswith("core/argus/") and path.suffix == ".py":
            mod = ".".join(path.with_suffix("").parts[1:])  # core/argus/worker/review.py -> argus.worker.review
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            picked |= {n for n, t in tests.items() if _imports(t, mod) or n == f"test_{path.stem}.py"
                       or (WIDE.match(rel) and ("Server(" in t or "Argus(" in t or "Worker(" in t))}
        elif rel.startswith("plugins/") and len(path.parts) > 2:
            name = path.parts[1]
            picked |= {n for n, t in tests.items() if name in t or n == f"test_{name.replace('-', '_')}.py"}
        elif rel.startswith(("helios/", "core/argus/helios_dist/")):
            picked |= {n for n in tests if "helios" in n}
        elif rel.startswith(("scripts/", "deploy/")):
            picked |= {n for n, t in tests.items() if path.name in t or path.stem in n}
    return sorted(picked)


def rebuild_helios() -> None:
    npm = shutil.which("npm")
    if not npm:
        raise ChangeError("Helios source changed but npm isn't installed: install Node.js, then run this again")
    web = ROOT / "helios"
    if not (web / "node_modules").exists():
        run(npm, "ci", cwd=web)
    run(npm, "run", "build", cwd=web)


def apply(package: Path, tests: bool = True, all_tests: bool = False) -> str:
    if not package.is_file():
        raise ChangeError(f"no such package: {package}")
    if git("status", "--porcelain"):
        raise ChangeError("you have uncommitted changes: commit or stash them first")
    with tarfile.open(package) as tar:
        try:
            meta = read_meta(tar.extractfile(META).read())  # type: ignore[union-attr]
        except KeyError:
            raise ChangeError(f"the package has no {META}: ask for a package made with change metadata") from None
        members = safe_members(tar)
        used = branch_used(meta["branch"])
        if used:
            raise ChangeError(f"branch {meta['branch']!r} can't be used: {used}. Ask for a new name.")
        print(f"main <- up to date, new branch {meta['branch']}")
        git("switch", "main")
        if "origin" in git("remote").split():
            git("pull", "--ff-only", "--quiet")
        git("switch", "-c", meta["branch"])
        tar.extractall(ROOT, members=members)  # noqa: S202 - members checked above
    for rel in meta.get("deleted") or []:
        p = ROOT / rel
        if p.is_file() and ROOT in p.resolve().parents:
            p.unlink()
    log = ROOT / "CHANGELOG.md"
    log.write_text(add_changelog(log.read_text(encoding="utf-8"), meta["changelog"]), encoding="utf-8")
    changed = git("status", "--porcelain")
    if re.search(r"^.. (?:helios/|core/argus/helios_dist/)", changed, re.M):
        print("Helios source changed: rebuilding")
        rebuild_helios()
    if tests:
        run(sys.executable, "-m", "ruff", "check", "core", "tests", "scripts")
        status = run("git", "status", "--porcelain", "-uall").stdout  # not git(): it strips the first line's space
        paths = [ln[3:].strip().strip('"').split(" -> ")[-1] for ln in status.splitlines() if ln.strip()]
        which = None if all_tests or os.environ.get("ARGUS_ALL_TESTS") else affected_tests(paths)
        if which == []:
            print("lint ok; no tests cover these files (CI runs them all)", flush=True)
        else:
            print("lint ok; " + ("all tests" if which is None else f"the tests for these files: {' '.join(which)}")
                  + " (CI runs them all)", flush=True)
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                *[f"tests/{n}" for n in which or []]], cwd=ROOT)
            if p.returncode != 0:
                raise ChangeError(f"tests failed (above), nothing committed; you are on {meta['branch']}")
    git("add", "-A")
    msg = ROOT / ".git" / "argus-change" / f"{meta['branch'].replace('/', '__')}.md"
    msg.parent.mkdir(parents=True, exist_ok=True)
    msg.write_text(meta["body"].strip() + "\n", encoding="utf-8")
    note = msg.with_suffix(".msg")
    note.write_text(commit_message(meta["title"], meta["body"]), encoding="utf-8")
    git("commit", "-q", "-F", str(note))
    print(f"Committed on {meta['branch']}: {meta['title']}")
    print("Next: .\\scripts\\dev.ps1 pr   then, when CI is green: .\\scripts\\dev.ps1 merge")
    return meta["branch"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply")
    a.add_argument("package", type=Path)
    a.add_argument("--no-tests", action="store_true")
    a.add_argument("--all-tests", action="store_true", help="the full suite, not only the changed files' tests")
    t = sub.add_parser("check-title")
    t.add_argument("title")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "apply":
            apply(args.package, tests=not args.no_tests, all_tests=args.all_tests)
        else:
            problem = title_problem(args.title)
            if problem:
                raise ChangeError(problem)
    except ChangeError as e:
        print(f"change: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
