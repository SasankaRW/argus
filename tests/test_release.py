"""The release tool and the pre-push hook, against a real throwaway Git repo and a local 'GitHub' remote."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", REPO / "scripts" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

CHANGELOG = "# Changelog\n\n## Unreleased\n\n- Added a thing\n\n## 0.4.0 (2026-09-28)\n\n- Old\n"


def test_bump():
    assert release.bump("0.4.0", "patch") == "0.4.1"
    assert release.bump("0.4.7", "minor") == "0.5.0"
    assert release.bump("0.4.7", "major") == "1.0.0"
    with pytest.raises(release.ReleaseError):
        release.bump("0.4", "patch")


def test_changelog_release():
    assert release.unreleased_notes(CHANGELOG) == "- Added a thing"
    out = release.apply_release(CHANGELOG, "0.5.0", "2026-10-01")
    assert out.index("## Unreleased") < out.index("## 0.5.0 (2026-10-01)") < out.index("## 0.4.0")
    assert release.unreleased_notes(out) == ""
    with pytest.raises(release.ReleaseError, match="Nothing under"):
        release.apply_release(out, "0.5.1", "2026-10-02")
    with pytest.raises(release.ReleaseError, match="already has"):
        release.apply_release(CHANGELOG.replace("0.4.0", "0.5.0"), "0.5.0", "x")
    with pytest.raises(release.ReleaseError, match="no '## Unreleased'"):
        release.unreleased_notes("# Changelog\n")


def git(cwd: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if not k.startswith("ARGUS_")}
    e.update({"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T",
              "GIT_COMMITTER_EMAIL": "t@t", **(env or {})})
    r = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, env=e)
    if check and r.returncode != 0:
        raise AssertionError(r.stderr)
    return r


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "VERSION").write_text("0.4.0\n")
    (work / "CHANGELOG.md").write_text(CHANGELOG)
    shutil.copytree(REPO / ".githooks", work / ".githooks")
    git(work, "config", "core.hooksPath", ".githooks")
    for k, v in {"user.name": "T", "user.email": "t@t"}.items():
        git(work, "config", k, v)
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "chore: init")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "-u", "origin", "main", env={"ARGUS_ALLOW_MAIN": "1"})
    monkeypatch.setattr(release, "ROOT", work)
    monkeypatch.delenv("ARGUS_RELEASE", raising=False)
    monkeypatch.delenv("ARGUS_ALLOW_MAIN", raising=False)
    return work, origin


def test_release_end_to_end(repo, monkeypatch):
    work, origin = repo
    monkeypatch.setattr(release.shutil, "which", lambda name: None)  # no gh in tests
    assert release.main(["minor", "--skip-tests"]) == 0
    assert (work / "VERSION").read_text().strip() == "0.5.0"
    assert "## 0.5.0 (" in (work / "CHANGELOG.md").read_text()
    assert git(origin, "tag", "--list").stdout.split() == ["v0.5.0"]
    assert git(origin, "log", "-1", "--format=%s", "main").stdout.strip() == "Release v0.5.0"
    msg = git(work, "tag", "-l", "--format=%(contents)", "v0.5.0").stdout
    assert "Added a thing" in msg
    # nothing new under Unreleased: a second release is refused
    assert release.main(["patch", "--skip-tests"]) == 1


def test_release_refuses_dirty_tree_and_other_branches(repo, capsys):
    work, _ = repo
    (work / "VERSION").write_text("0.4.0\n\n")
    assert release.main(["patch", "--skip-tests"]) == 1
    assert "uncommitted" in capsys.readouterr().err
    git(work, "checkout", "-q", "--", "VERSION")
    git(work, "switch", "-q", "-c", "feat/x")
    assert release.main(["patch", "--skip-tests"]) == 1
    assert "from main" in capsys.readouterr().err


def test_dry_run_changes_nothing(repo):
    work, _ = repo
    assert release.main(["major", "--dry-run"]) == 0
    assert (work / "VERSION").read_text().strip() == "0.4.0"


@pytest.mark.skipif(os.name == "nt" and not shutil.which("sh"), reason="hook needs sh")
def test_hook_protects_main(repo):
    work, _ = repo
    (work / "a.txt").write_text("x")
    git(work, "add", "a.txt")
    git(work, "commit", "-q", "-m", "chore: direct")
    blocked = git(work, "push", "origin", "main", check=False)
    assert blocked.returncode != 0 and "pull request" in blocked.stderr
    git(work, "push", "-q", "origin", "main", env={"ARGUS_ALLOW_MAIN": "1"})  # explicit override works
    git(work, "reset", "-q", "--hard", "HEAD~1")
    forced = git(work, "push", "-f", "origin", "main", env={"ARGUS_ALLOW_MAIN": "1"}, check=False)
    assert forced.returncode != 0 and "force-push" in forced.stderr
    gone = git(work, "push", "origin", ":main", env={"ARGUS_ALLOW_MAIN": "1"}, check=False)
    assert gone.returncode != 0 and "delete main" in gone.stderr
    git(work, "switch", "-q", "-c", "feat/y")
    assert git(work, "push", "-q", "origin", "feat/y", check=False).returncode == 0  # branches are free
