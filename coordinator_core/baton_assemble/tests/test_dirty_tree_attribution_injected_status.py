"""`_compute_dirty_tree_attribution(root, dirty=...)` returns what the spawning form returns, spawning nothing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
from coordinator_core.pickup_assemble.tests._git_harness import (
    git as _git,
    init_repo as _init_repo,
)
from coordinator_core.session.session_facts import _dirty_paths
from coordinator_core.session.touch_record import append_event

SESSION = "sess-injected-dirty"


@pytest.fixture(autouse=True)
def _session(monkeypatch):
    for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SESSION)
    monkeypatch.setattr(
        "coordinator_core.session.touch_record.session_live",
        lambda sid, cwd=None: True,
    )


def _fixture_repo(repo: Path) -> None:
    _init_repo(repo)
    (repo / "kept.txt").write_text("a\n", encoding="utf-8")
    (repo / "old_name.txt").write_text("rename me\n", encoding="utf-8")
    _git(repo, "add", "kept.txt", "old_name.txt")
    _git(repo, "commit", "-m", "base")
    (repo / "kept.txt").write_text("modified\n", encoding="utf-8")
    _git(repo, "mv", "old_name.txt", "new_name.txt")
    (repo / "fresh" / "dir").mkdir(parents=True)
    (repo / "fresh" / "dir" / "new.md").write_text("n\n", encoding="utf-8")
    (repo / "peer.txt").write_text("peer\n", encoding="utf-8")
    sink = ba.git_common_dir(repo) / "coordinator-sessions" / SESSION / "touch-record.jsonl"
    for path in ("kept.txt", "new_name.txt", "fresh/dir/new.md"):
        append_event(sink, session_id=SESSION, agent_id=None, verb="T", path=path)


def test_injected_equals_spawning(tmp_path):
    _fixture_repo(tmp_path)
    spawned = ba._compute_dirty_tree_attribution(tmp_path)
    injected = ba._compute_dirty_tree_attribution(tmp_path, dirty=_dirty_paths(tmp_path))
    assert spawned["degraded"] is False
    assert spawned["mine"] == ["fresh/dir/new.md", "kept.txt", "new_name.txt"]
    assert spawned["residue_count"] >= 1
    assert injected == spawned


def test_injected_spawns_nothing(tmp_path, monkeypatch):
    _fixture_repo(tmp_path)
    record = _dirty_paths(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("subprocess spawned with dirty injected")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    result = ba._compute_dirty_tree_attribution(tmp_path, dirty=record)
    assert result["degraded"] is False
    assert "kept.txt" in result["mine"]


def test_degraded_record_yields_degraded(tmp_path):
    _fixture_repo(tmp_path)
    record = {"degraded": True, "evidence": "git status exited 128", "source": "x"}
    result = ba._compute_dirty_tree_attribution(tmp_path, dirty=record)
    assert result["degraded"] is True
    assert "git status exited 128" in result["evidence"]
