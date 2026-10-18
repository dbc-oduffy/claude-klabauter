"""terminal_commit's full path reads the worktree once and commits the plan file once.

The product commit, review-stamp mint, coded-stamp and implemented flip share one
worktree snapshot (read in-process, no `git status` spawn) and one close commit; a close whose coded-stamp commit fails
leaves the plan's status unflipped on disk.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git_out(repo, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _status_line(repo) -> str:
    text = (repo / driver.PLAN_REL).read_text(encoding="utf-8")
    return next(ln for ln in text.splitlines() if ln.startswith("status:"))


def test_the_full_path_spawns_one_status_read_and_commits_the_plan_once(tmp_path, monkeypatch):
    repo, script = driver.build_fixture(tmp_path)
    spawned: list = []
    real_init = subprocess.Popen.__init__

    def counting_init(self, args, *a, **kw):
        spawned.append(list(args) if isinstance(args, (list, tuple)) else [args])
        real_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting_init)
    out = driver._run_full_path(repo, script)
    monkeypatch.undo()

    assert out["review_stamp"] == "minted", out
    assert out["plan_status"] == "implemented", out
    assert out["coded_sha"], out
    git_calls = [c for c in spawned if c and Path(str(c[0])).stem.lower() == "git"]
    statuses = [c for c in git_calls if "status" in c]
    # The worktree read is answered in-process by scoped_worktree_status; the status spawn is
    # only its fallback, so a clean fixture spawns no git at all.
    assert len(statuses) == 0, git_calls
    assert len(git_calls) == 0, git_calls
    assert _status_line(repo) == "status: implemented"
    plan_commits = _git_out(repo, "log", "--format=%h", "--", driver.PLAN_REL).split()
    assert len(plan_commits) == 2, plan_commits  # the fixture's own plan commit plus the one close commit
    assert _git_out(repo, "show", f"HEAD:{driver.PLAN_REL}").count("status: implemented") == 1
    assert _git_out(repo, "status", "--porcelain", "--", driver.PLAN_REL).strip() == ""
    assert out["undeclared_dirty"] == [script]  # only the emitted script; nothing the close landed


def test_a_failed_coded_stamp_commit_leaves_the_plan_status_unflipped_on_disk(tmp_path, monkeypatch):
    repo, script = driver.build_fixture(tmp_path)
    real_dispatch = terminal_commit.reentrant_dispatch
    calls: list = []

    def failing_second_commit(op, params, repo_root=None):
        calls.append(op)
        if len(calls) == 2:
            return {"committed": False, "error": "forced"}
        return real_dispatch(op, params, repo_root=repo_root)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", failing_second_commit)
    out = driver._run_full_path(repo, script)

    assert len(calls) == 2, calls
    assert "coded_stamp_error" in out, out
    assert out["plan_status"] == "not-stamped", out
    assert _status_line(repo) == "status: executing"
    assert "disposition: open" in (repo / driver.PLAN_REL).read_text(encoding="utf-8")
