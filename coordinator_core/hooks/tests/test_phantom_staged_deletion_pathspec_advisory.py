"""Pins the phantom-staged-deletion pre-commit gate end to end in a real tmp_path repo:
a pathspec commit ignores an unrelated staged D, and a plain commit gets an advisory, never a denial."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.hooks import phantom_staged_deletion

REPO_ROOT = Path(__file__).resolve().parents[3]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
        PYTHONPATH=str(REPO_ROOT),
    )
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, env=_env(), capture_output=True, text=True,
        creationflags=NO_WINDOW,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    for name in ("phantom.md", "other.md", "third.md"):
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    assert _git(root, "add", ".").returncode == 0
    assert _git(root, "commit", "-q", "-m", "base").returncode == 0
    hook = root / ".git" / "hooks" / "pre-commit"
    py = Path(sys.executable).as_posix()
    hook.write_text(
        "#!/bin/sh\n"
        f'exec "{py}" -c "import sys; from coordinator_core.hooks.'
        'guard_phantom_staged_deletion_precommit import main; sys.exit(main())"\n',
        encoding="utf-8",
    )
    hook.chmod(0o755)
    # Stale-index phantom: D staged, file still in HEAD and on disk.
    assert _git(root, "rm", "--cached", "-q", "phantom.md").returncode == 0
    assert (root / "phantom.md").exists()
    return root


def _committed_paths(repo: Path) -> list[str]:
    out = _git(repo, "show", "--name-status", "--format=", "HEAD").stdout
    return sorted(line.replace("\t", " ") for line in out.splitlines() if line)


def test_pathspec_commit_with_unrelated_staged_deletion_passes(repo: Path):
    (repo / "other.md").write_text("changed\n", encoding="utf-8")
    res = _git(repo, "commit", "-q", "-m", "x", "--", "other.md")
    assert res.returncode == 0, res.stderr
    assert _committed_paths(repo) == ["M other.md"]
    assert _git(repo, "cat-file", "-e", "HEAD:phantom.md").returncode == 0


def test_plain_commit_with_phantom_deletion_advises_and_exits_zero(repo: Path):
    (repo / "other.md").write_text("changed\n", encoding="utf-8")
    assert _git(repo, "add", "other.md").returncode == 0
    res = _git(repo, "commit", "-q", "-m", "x")
    assert res.returncode == 0, res.stderr
    assert "ADVISORY" in res.stderr
    assert "phantom.md" in res.stderr


def test_report_never_suggests_git_reset():
    finding = phantom_staged_deletion.Finding(path="x.md", disk_matches_head=True)
    report = phantom_staged_deletion.render_report([finding], "OVERRIDE_X")
    assert "git reset" not in report
    assert "x.md" in report
