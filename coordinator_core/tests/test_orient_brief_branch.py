"""Branch family of orient_brief: one test per requirement id (REQ-B1..B3)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.daily_day import local_day
from coordinator_core.orient_brief import _branch

FORBIDDEN_GIT_VERBS = ("status", "diff-index", "diff", "ls-files", "fetch")


@pytest.fixture(autouse=True)
def _machine(monkeypatch):
    monkeypatch.setenv("COORDINATOR_MACHINE", "box")


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    """The family is zero-spawn; any process creation fails the test."""
    def boom(*a, **k):
        raise AssertionError(f"branch family spawned a process: {a!r}")
    monkeypatch.setattr(subprocess, "Popen", boom)


def _repo(tmp_path: Path, head: str) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(head + "\n", encoding="utf-8")
    return tmp_path


def _on(tmp_path: Path, branch: str) -> Path:
    return _repo(tmp_path, f"ref: refs/heads/{branch}")


@pytest.mark.parametrize("cadence", ["session", "day", "week"])
def test_req_b1_stale_span_emits_directive_at_every_cadence(tmp_path, cadence):
    root = _on(tmp_path, "work/box/2020-01-01to03")
    result = _branch.collect(cadence, repo_root=root)
    assert result.judgment_points == []
    (d,) = result.directives
    today = local_day(str(root))
    expected = "work/box/2020-01-01to" + today.rsplit("-", 1)[-1]
    assert d["id"] == "d-branch-span-mismatch"
    assert d["cli"] == "workday-start-day-branch-resolve"
    assert d["args"] == ["span-assert"]
    assert d["depends_on"] is None and d["already_satisfied"] is False
    assert "`work/box/2020-01-01to03`" in d["detail"]
    assert f"({today})" in d["detail"]
    assert f"`{expected}`" in d["detail"]


def test_req_b1_single_day_branch_in_the_past_is_a_mismatch(tmp_path):
    root = _on(tmp_path, "work/box/2020-01-01")
    (d,) = _branch.collect("day", repo_root=root).directives
    today = local_day(str(root))
    assert f"expected rename to `work/box/2020-01-01to{today[-2:]}`" in d["detail"]


def test_req_b1_span_ending_today_is_silent(tmp_path):
    today = local_day(str(tmp_path))
    root = _on(tmp_path, f"work/box/{today}")
    assert _branch.collect("day", repo_root=root) == _branch.ReaderResult()


@pytest.mark.parametrize("branch", ["main", "feature/foo", "work/box/feature-x"])
def test_req_b1_non_span_branch_is_silent(tmp_path, branch):
    root = _on(tmp_path, branch)
    assert _branch.collect("day", repo_root=root) == _branch.ReaderResult()


def test_req_b1_detached_head_is_silent(tmp_path):
    root = _repo(tmp_path, "0123456789abcdef0123456789abcdef01234567")
    assert _branch.collect("day", repo_root=root) == _branch.ReaderResult()


def test_req_b1_no_git_dir_is_silent(tmp_path):
    assert _branch.collect("day", repo_root=tmp_path) == _branch.ReaderResult()


def test_req_b1_gitlink_worktree_resolves_head(tmp_path):
    private = tmp_path / "gitdirs" / "wt"
    private.mkdir(parents=True)
    (private / "HEAD").write_text("ref: refs/heads/work/box/2020-01-01\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {private}\n", encoding="utf-8")
    (d,) = _branch.collect("day", repo_root=wt).directives
    assert d["id"] == "d-branch-span-mismatch"


def test_req_b2_retired_emits_nothing(tmp_path):
    # No reconcile id exists in the rebuilt family.
    src = Path(_branch.__file__).read_text(encoding="utf-8")
    assert "reconcile" not in re.sub(r'""".*?"""', "", src, flags=re.S).lower()


def test_req_b3_retired_family_runs_no_worktree_read(tmp_path):
    """AC12: no status/diff-index/diff/ls-files/fetch, and no subprocess at all."""
    src = Path(_branch.__file__).read_text(encoding="utf-8")
    body = re.sub(r'""".*?"""', "", src, flags=re.S)
    assert "subprocess" not in body
    for verb in FORBIDDEN_GIT_VERBS:
        assert not re.search(rf'["\']{re.escape(verb)}["\']', body), verb
    root = _on(tmp_path, "work/box/2020-01-01")
    _branch.collect("day", repo_root=root)  # _no_spawn fixture is the runtime pin
