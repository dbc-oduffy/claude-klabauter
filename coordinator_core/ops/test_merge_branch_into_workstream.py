"""Tests for coordinator_core.ops.merge_branch_into_workstream (op
`branch.merge_into_workstream`, settlement A4).

All git activity runs in tmp_path throwaway repos.

Coverage map (settlement A4):
  - merge path: divergent branch → --no-ff merge commit, merged:true
  - double-invocation (CC-4/AC7): rerun after success short-circuits at the
    `_is_ancestor` pre-check → {merged: false, already_ancestor: true}
  - conflict path: `git merge --abort` restores a clean tree, conflict:true
  - override env vars travel via subprocess env= dict, never inline-env argv
  - CC-7: merge failure with no merge-in-progress raises, not conflict:true
  - fail-loud: unknown branch / non-repo root / handler without repo_root
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import merge_branch_into_workstream as mbw


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _make_divergent_branch(repo: Path, name: str = "work/other/2026-07-21") -> None:
    """Create *name* with a commit, then advance main so the merge is real
    (a fast-forwardable branch would not exercise --no-ff meaningfully)."""
    _git(repo, "checkout", "-q", "-b", name)
    (repo / "branch.txt").write_text("branch work\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "branch work")
    _git(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("main work\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main work")


def test_merge_creates_no_ff_merge_commit(tmp_path):
    repo = _init_repo(tmp_path)
    _make_divergent_branch(repo)
    out = mbw.merge_branch_into_workstream(
        "work/other/2026-07-21", "daily consolidation", repo
    )
    assert out == {"merged": True, "already_ancestor": False, "conflict": False}
    # --no-ff: HEAD is a 2-parent merge commit with the fence's message.
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3
    subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "consolidate work/other/2026-07-21 into active workstream branch"


def test_double_invocation_short_circuits_already_ancestor(tmp_path):
    """CC-4/AC7: the successful merge makes the branch an ancestor; the
    rerun returns the documented no-op shape without invoking merge."""
    repo = _init_repo(tmp_path)
    _make_divergent_branch(repo)
    first = mbw.merge_branch_into_workstream("work/other/2026-07-21", "r", repo)
    assert first["merged"] is True
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    second = mbw.merge_branch_into_workstream("work/other/2026-07-21", "r", repo)
    assert second == {"merged": False, "already_ancestor": True, "conflict": False}
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_already_ancestor_without_prior_op_merge(tmp_path):
    """A branch merged by any other means is still an already_ancestor no-op."""
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature/x")
    (repo / "x.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "x")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "feature/x")
    out = mbw.merge_branch_into_workstream("feature/x", "r", repo)
    assert out == {"merged": False, "already_ancestor": True, "conflict": False}


def test_conflict_aborts_and_reports(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "work/other/2026-07-21")
    (repo / "f.txt").write_text("branch version\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "branch edit")
    _git(repo, "checkout", "-q", "main")
    (repo / "f.txt").write_text("main version\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main edit")

    out = mbw.merge_branch_into_workstream("work/other/2026-07-21", "r", repo)
    assert out == {"merged": False, "already_ancestor": False, "conflict": True}
    # Abort restored a clean tree: no MERGE_HEAD, no dirty/conflicted entries.
    assert not mbw._merge_head_path(repo).exists()
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert (repo / "f.txt").read_text() == "main version\n"


def test_override_env_vars_travel_via_env_dict_not_argv(tmp_path, monkeypatch):
    """Settlement A4: COORDINATOR_OVERRIDE_BRANCH* pass via subprocess
    env= dict; no POSIX inline-env token ever appears in argv."""
    repo = _init_repo(tmp_path)
    _make_divergent_branch(repo)
    calls: list[tuple[list[str], dict | None]] = []
    real_git = mbw._git

    def spy(args, cwd=None, env=None):
        calls.append((list(args), env))
        return real_git(args, cwd=cwd, env=env)

    monkeypatch.setattr(mbw, "_git", spy)
    out = mbw.merge_branch_into_workstream(
        "work/other/2026-07-21", "consolidation reason", repo
    )
    assert out["merged"] is True
    merge_calls = [(a, e) for a, e in calls if a and a[0] == "merge"]
    assert merge_calls, "merge was never invoked"
    args, env = merge_calls[0]
    assert env is not None
    assert env["COORDINATOR_OVERRIDE_BRANCH"] == "1"
    assert env["COORDINATOR_OVERRIDE_BRANCH_REASON"] == "consolidation reason"
    for a, _e in calls:
        for token in a:
            assert not token.startswith("COORDINATOR_OVERRIDE_BRANCH"), (
                f"inline-env-style token leaked into argv: {a}"
            )


def test_non_conflict_merge_failure_raises_not_conflict(tmp_path):
    """CC-7: dirty-worktree merge refusal has no MERGE_HEAD — structured
    error, never mislabeled conflict:true."""
    repo = _init_repo(tmp_path)
    _make_divergent_branch(repo)
    # Local uncommitted edit to a file the merge must touch → git refuses
    # up-front without starting a merge (no MERGE_HEAD).
    (repo / "branch.txt").write_text("uncommitted local edit\n")
    _git(repo, "add", "branch.txt")
    with pytest.raises(RuntimeError, match="no merge in progress"):
        mbw.merge_branch_into_workstream("work/other/2026-07-21", "r", repo)


def test_unknown_branch_fails_loud(tmp_path):
    repo = _init_repo(tmp_path)
    with pytest.raises(ValueError, match="does not resolve"):
        mbw.merge_branch_into_workstream("work/ghost/2026-01-01", "r", repo)


def test_empty_branch_fails_loud(tmp_path):
    repo = _init_repo(tmp_path)
    with pytest.raises(ValueError, match="branch"):
        mbw.merge_branch_into_workstream("", "r", repo)


def test_non_repo_root_fails_loud(tmp_path):
    d = tmp_path / "notarepo"
    d.mkdir()
    with pytest.raises(ValueError, match="not a git worktree"):
        mbw.merge_branch_into_workstream("main", "r", d)


def test_handler_registered_and_uses_injected_repo_root(tmp_path):
    repo = _init_repo(tmp_path)
    _make_divergent_branch(repo)
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler("branch.merge_into_workstream")
    assert handler is not None
    out = asyncio.run(
        handler({"branch": "work/other/2026-07-21", "reason": "r"}, repo)
    )
    assert out == {"merged": True, "already_ancestor": False, "conflict": False}


def test_handler_without_repo_root_fails_loud():
    with pytest.raises(ValueError, match="repo_root"):
        asyncio.run(
            mbw._merge_branch_into_workstream_handler({"branch": "x", "reason": "r"}, None)
        )
