"""The dry-run's destination check refuses a clone behind its upstream or dirty, and writes nothing."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))

from percolate.dest_refresh import RefreshResult, dry_run_refusal, fetch_dest_upstream  # noqa: E402

pytestmark = [pytest.mark.spawns_process]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, **_NO_CONSOLE
    )
    return proc.stdout.strip()


def _commit(root: Path, name: str) -> None:
    (root / name).write_text(name, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-m", name)


@pytest.fixture()
def origin_and_clone(tmp_path: Path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "candidate")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    _commit(origin, "seed")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--branch", "candidate", str(origin), str(clone))
    return origin, clone


def _check(clone: Path):
    return dry_run_refusal(
        fetch_dest_upstream(clone, out=io.StringIO(), err=io.StringIO())
    )


def test_level_and_clean_clone_is_accepted(origin_and_clone):
    _, clone = origin_and_clone
    assert _check(clone) is None


def test_clone_behind_upstream_is_refused_and_unchanged(origin_and_clone):
    origin, clone = origin_and_clone
    _commit(origin, "newer")
    head_before = _git(clone, "rev-parse", "HEAD")
    reason = _check(clone)
    assert reason is not None and "behind" in reason and str(clone) in reason
    assert _git(clone, "rev-parse", "HEAD") == head_before
    assert not (clone / "newer").exists()


def test_dirty_tracked_clone_is_refused_and_unchanged(origin_and_clone):
    _, clone = origin_and_clone
    (clone / "seed").write_text("live edit", encoding="utf-8")
    reason = _check(clone)
    assert reason is not None and "uncommitted" in reason and "seed" in reason
    assert (clone / "seed").read_text(encoding="utf-8") == "live edit"


def test_untracked_file_alone_does_not_refuse(origin_and_clone):
    _, clone = origin_and_clone
    (clone / "scratch.txt").write_text("x", encoding="utf-8")
    assert _check(clone) is None


@pytest.mark.parametrize("flag", [{"pending_abort": True}, {"subsumed": ("peer",)}])
def test_pending_merge_with_zero_behind_is_refused_without_a_behind_count(tmp_path, flag):
    result = RefreshResult(
        repo_root=tmp_path, ok=True, branch="candidate", upstream="origin/candidate", behind=0, **flag
    )
    reason = dry_run_refusal(result)
    assert reason is not None and "merge step" in reason
    assert "0 commit(s) behind" not in reason and "--ff-only" not in reason


def test_diverged_clone_is_refused_and_unchanged(origin_and_clone):
    origin, clone = origin_and_clone
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    _commit(clone, "local-only")
    _commit(origin, "upstream-only")
    head_before = _git(clone, "rev-parse", "HEAD")
    reason = _check(clone)
    assert reason is not None
    assert _git(clone, "rev-parse", "HEAD") == head_before
