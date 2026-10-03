"""After dispatch.terminal_commit lands a path, the index entry for it equals the landed blob.

One case per hypothesis for the stale-index-after-terminal-commit defect: H3 plain shape,
H1 splice failure (lock busy once; v4 index), H2 second writer (coded-stamp follow-up commit).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import index_write
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0}

_PLAN = """---
title: p
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _call(repo: Path, params: dict) -> dict:
    return terminal_commit._handler({"inline_review": _REVIEWED, **params}, repo_root=repo / ".git")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "a.py").write_text("head\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "plan.md").write_text(_PLAN, encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _script(repo: Path, request: CommitRequest) -> str:
    marker = render_marker(request)
    assert marker is not None, "fixture request has no committable chunk"
    (repo / "run.mjs").write_text("// emitted\n" + marker + "\n", encoding="utf-8")
    return "run.mjs"


def _stage_differing(repo: Path, rel: str, staged: str, worktree: str) -> None:
    (repo / rel).write_text(staged, encoding="utf-8")
    _git(["add", rel], repo)
    (repo / rel).write_text(worktree, encoding="utf-8")


def _assert_index_at_head(repo: Path, rels: list) -> None:
    assert _git(["diff", "--cached", "--name-only", "--", *rels], repo) == ""
    for rel in rels:
        assert _git(["ls-files", "-s", "--", rel], repo).split()[1] == _git(
            ["rev-parse", f"HEAD:{rel}"], repo
        ).strip()


def _run_plain(repo: Path) -> dict:
    _stage_differing(repo, "a.py", "staged\n", "worktree\n")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),))
    out = _call(repo, {"script_path": _script(repo, request), "incomplete_chunks": []})
    assert out["committed"] is True, out
    assert _git(["show", "HEAD:a.py"], repo) == "worktree\n"
    return out


def test_h3_plain_differing_index_ends_at_head(repo):
    """H3: the plain shape (staged blob differs from HEAD and worktree)."""
    _run_plain(repo)
    _assert_index_at_head(repo, ["a.py"])


def test_h1_lock_busy_once_still_ends_at_head(repo, monkeypatch):
    """H1: splice_index raises IndexWriteLockBusy on its first call."""
    real = index_write.splice_index
    calls = {"n": 0}

    def flaky(repo_path, updates):
        calls["n"] += 1
        if calls["n"] == 1:
            raise index_write.IndexWriteLockBusy("simulated peer lock")
        return real(repo_path, updates)

    monkeypatch.setattr(index_write, "splice_index", flaky)
    _run_plain(repo)
    assert calls["n"] == 2
    _assert_index_at_head(repo, ["a.py"])


def test_h1_v4_index_refuses_before_landing(repo):
    """H1: a version-4 index is refused before the ref moves, never committed over a stale index."""
    _git(["update-index", "--index-version", "4"], repo)
    _stage_differing(repo, "a.py", "staged\n", "worktree\n")
    head = _git(["rev-parse", "HEAD"], repo)
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),))
    out = _call(repo, {"script_path": _script(repo, request), "incomplete_chunks": []})
    assert out["committed"] is False, out
    assert "index-version 2" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_persistent_splice_failure_reports_index_stale(repo, monkeypatch):
    """A splice that keeps failing yields committed: true, the sha, and index_stale naming the paths."""
    calls = {"n": 0}

    def busy(repo_path, updates):
        calls["n"] += 1
        raise index_write.IndexWriteLockBusy("simulated peer lock")

    monkeypatch.setattr(index_write, "splice_index", busy)
    out = _run_plain(repo)
    assert calls["n"] == 2
    assert out["sha"]
    assert out["index_stale"] == ["a.py"]
    assert "git add" in " ".join(out["warnings"])


def test_h2_coded_stamp_follow_up_commit_ends_at_head(repo):
    """H2: the coded-stamp second commit_v2 runs over a plan whose staged blob differs."""
    _stage_differing(repo, "docs/plan.md", _PLAN + "\nstaged\n", _PLAN)
    _stage_differing(repo, "a.py", "staged\n", "worktree\n")
    request = CommitRequest(
        chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),),
        plan_path="docs/plan.md",
    )
    out = _call(repo, {"script_path": _script(repo, request), "incomplete_chunks": []})
    assert out["committed"] is True, out
    assert out.get("coded_sha"), out
    _assert_index_at_head(repo, ["a.py", "docs/plan.md"])
