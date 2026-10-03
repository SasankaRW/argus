"""The change tool (scripts/change.py) and the commit-msg hook, against a real throwaway Git repo."""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("change", REPO / "scripts" / "change.py")
change = importlib.util.module_from_spec(spec)
spec.loader.exec_module(change)

BODY = "## What\nA thing.\n\n## Why\nBecause.\n\n## How tested\nA test.\n\n## Risk\nLow."
META = {"branch": "feat/a-thing", "title": "feat(core): add a thing", "body": BODY,
        "changelog": "- **A thing:** it does it.", "deleted": ["old.txt"]}


@pytest.mark.parametrize("title, ok", [
    ("feat(ari): answer weather without the model", True),
    ("fix: stop the phone claiming other jobs", True),
    ("perf(screen)!: read text with OCR first", True),
    ("feat(api): API tokens per device", True),
    ("Added stuff", False),
    ("feat: Add a thing", False),
    ("feat: added a thing", False),
    ("feat: add a thing.", False),
    ("feature: add a thing", False),
    ("feat: " + "x" * 70, False),
])
def test_titles(title, ok):
    assert (change.title_problem(title) is None) is ok


def test_branch_and_body_rules():
    assert change.branch_problem("feat/ari-direct-paths") is None
    assert change.branch_problem("Feature/Thing") and change.branch_problem("feat/x")
    assert change.body_problem(BODY) is None
    assert "How tested" in change.body_problem("## What\nx\n## Why\ny\n## Risk\nz")
    assert change.body_problem(BODY + "\n\nCo-Authored-By: Claude <x@y>")


def test_changelog_goes_under_unreleased():
    log = "# Changelog\n\n## Unreleased\n\n- Old line\n\n## 0.4.0\n"
    out = change.add_changelog(log, "- New line")
    assert out == "# Changelog\n\n## Unreleased\n\n- New line\n\n- Old line\n\n## 0.4.0\n"
    with pytest.raises(change.ChangeError):
        change.add_changelog("# Changelog\n", "- x")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def package(path: Path, meta: dict, files: dict[str, str]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, text in {**files, ".change/change.json": json.dumps(meta)}.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    shutil.copytree(REPO / ".githooks", root / ".githooks")
    for hook in (root / ".githooks").iterdir():  # Windows checkouts may lose the executable bit
        hook.chmod(0o755)
    git(root, "config", "core.hooksPath", ".githooks")
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n## 0.1.0\n", encoding="utf-8")
    (root / "old.txt").write_text("bye", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "chore: start")
    monkeypatch.setattr(change, "ROOT", root)
    return root


def test_apply_makes_one_clear_commit_on_a_new_branch(repo, tmp_path):
    pkg = package(tmp_path / "p.tgz", META, {"core/thing.py": "x = 1\n"})
    assert change.apply(pkg, tests=False) == "feat/a-thing"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feat/a-thing"
    assert git(repo, "log", "-1", "--format=%s") == "feat(core): add a thing"
    assert "## How tested" in git(repo, "log", "-1", "--format=%b")
    assert (repo / "core/thing.py").exists() and not (repo / "old.txt").exists()
    assert not (repo / ".change").exists()
    assert "- **A thing:** it does it." in (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / ".git/argus-change/feat__a-thing.md").read_text(encoding="utf-8").startswith("## What")


def test_apply_refuses_a_used_branch_a_dirty_tree_and_bad_paths(repo, tmp_path):
    git(repo, "branch", "feat/a-thing")
    with pytest.raises(change.ChangeError, match="local branch"):
        change.apply(package(tmp_path / "a.tgz", META, {"x.py": ""}), tests=False)
    with pytest.raises(change.ChangeError, match="unsafe"):
        change.apply(package(tmp_path / "b.tgz", {**META, "branch": "feat/b-thing"}, {"../evil.py": ""}),
                     tests=False)
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    with pytest.raises(change.ChangeError, match="uncommitted"):
        change.apply(package(tmp_path / "c.tgz", {**META, "branch": "feat/c-thing"}, {}), tests=False)
    with pytest.raises(change.ChangeError, match="type\\(scope\\)"):
        change.read_meta(json.dumps({**META, "title": "Stuff"}).encode())


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell for the hook")
def test_the_commit_msg_hook(repo):
    (repo / "a.txt").write_text("a", encoding="utf-8")
    git(repo, "add", "-A")
    bad = subprocess.run(["git", "commit", "-qm", "updated things."], cwd=repo, capture_output=True, text=True)
    assert bad.returncode != 0 and "type(scope)" in bad.stderr
    claude = subprocess.run(["git", "commit", "-qm", "fix: a thing\n\nClaude-Session: x"], cwd=repo,
                            capture_output=True, text=True)
    assert claude.returncode != 0 and "attribution" in claude.stderr
    git(repo, "commit", "-qm", "fix(core): a thing\n\nWhy it was wrong.")
