"""plan.stamp_prepped puts a generated plan in HEAD (DoE 87e5c9: C0's prerequisite "the plan is in HEAD"
blocked on an untracked plan and held C1 unstarted); a plan HEAD already tracks is not committed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import plan_stamp_prepped as mod
from coordinator_core.ops.tests.test_plan_stamp_prepped import BY, REL, _plan
from coordinator_core.win_portability import no_console_creationflags


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo),
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "coordinator_core").mkdir()
    (tmp_path / "seed.txt").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    _plan(tmp_path)
    return tmp_path


def test_untracked_plan_is_in_head_with_its_attest_after_stamping(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _git(repo, "ls-tree", "HEAD", "--", REL).strip() == ""

    out = mod._handler({"plan": REL, "by": BY}, repo / ".git")

    assert out["outcome"] == "stamped" and "plan_commit" not in out
    assert REL in _git(repo, "ls-tree", "HEAD", "--", REL)
    assert "mise_prepped_sha" in _git(repo, "show", f"HEAD:{REL}")
    assert _git(repo, "status", "--porcelain", "--", REL).strip() == ""


def test_a_plan_head_already_tracks_is_not_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    _git(repo, "add", REL)
    _git(repo, "commit", "-q", "-m", "plan")
    head = _git(repo, "rev-parse", "HEAD")

    with _count_spawns_attributed(monkeypatch) as spawns:
        out = mod._handler({"plan": REL, "by": BY}, repo / ".git")

    assert out["outcome"] == "stamped"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert [s.argv[1] for s in spawns] == ["ls-tree"], [s.argv for s in spawns]


def test_a_restamp_of_a_certified_plan_commits_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    mod._handler({"plan": REL, "by": BY}, repo / ".git")
    head = _git(repo, "rev-parse", "HEAD")

    with _count_spawns_attributed(monkeypatch) as spawns:
        out = mod._handler({"plan": REL, "by": BY}, repo / ".git")

    assert out["outcome"] == "already-certified"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert spawns == []
