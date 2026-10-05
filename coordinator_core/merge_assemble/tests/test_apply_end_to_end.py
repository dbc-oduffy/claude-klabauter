"""
coordinator_core.merge_assemble.tests.test_apply_end_to_end — rehearses
`merge_assemble.apply` through its op dispatch against a throwaway git repo,
the `--force`-free path, with every judgment point the first pass returns
resolved.

Real git, real in-process CLI dispatch: no stubs between the op and the repo.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.merge_assemble import ops as ma_ops

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
    )
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def feature_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    (repo / "coordinator.local.md").write_text("---\ntag_prefix: v\n---\n", encoding="utf-8")
    _git(repo, "add", "base.txt", "coordinator.local.md")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "tag", "v0.1.0")
    _git(repo, "checkout", "-q", "-b", "feature/x")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "feature work")
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True,
        creationflags=_NO_WINDOW,
    )
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "feature/x")
    _git(repo, "push", "-q", "origin", "main", "--tags")
    _git(repo, "checkout", "-q", "feature/x")
    return repo


_DECISIONS = {
    "ship_verdict": {"disposition": "ship"},
    "version_bump_final": {"disposition": "confirmed"},
}


def test_apply_runs_every_directive_to_completion(
    feature_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    result = asyncio.run(
        ma_ops._merge_assemble_apply(
            {"session_id": "k10-e2e", "decisions": _DECISIONS}, feature_repo
        )
    )
    report = result["report"]
    assert result["exit_code"] == 0, report
    assert _git(feature_repo, "status", "--porcelain") == ""
    assert report["release_tag_cut"] == "v0.1.1"
    merged_sha = _git(feature_repo, "rev-parse", "origin/main")
    assert _git(feature_repo, "rev-parse", "v0.1.1^{commit}") == merged_sha
    assert _git(feature_repo, "merge-base", "--is-ancestor", "feature/x", "main") == ""
    assert "v0.1.1" in _git(feature_repo, "ls-remote", "--tags", "origin").replace("refs/tags/", "")
