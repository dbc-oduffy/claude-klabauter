"""REQ-W9 agent-worktree rows of `orient_brief._work`, against a real git repository.

Split from `test_orient_brief_work.py` so the spawn tiers onto cadence without
moving that file's in-process rows."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.orient_brief import _work
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _by_id(entries: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in entries}


@pytest.fixture(autouse=True)
def _quiet_environment(monkeypatch, tmp_path_factory):
    empty = tmp_path_factory.mktemp("harness-env")
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setenv("USERPROFILE", str(empty))
    monkeypatch.setenv("CLAUDE_HOME", str(empty))
    monkeypatch.setenv("COORDINATOR_PLUGINS_ROOT", str(empty / "plugins"))
    monkeypatch.setenv("COORDINATOR_CONSUMER_HEALTH_ROOT", str(empty / "consumer"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(empty))
    (empty / ".claude").mkdir()
    (empty / ".claude" / "settings.json").write_text(json.dumps({"effortLevel": "medium"}))
    monkeypatch.setattr("coordinator_core.ops.check_rag_state.check_rag_state", lambda: ("fresh", 0))
    monkeypatch.setattr("coordinator_core.machine_profile.feature_enabled", lambda name: False)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def real_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "f.txt").write_text("x")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-m", "init")
    return root


def test_req_w9_agent_worktree_states(real_repo):
    clean = real_repo / ".claude" / "worktrees" / "agent-clean"
    dirty = real_repo / ".claude" / "worktrees" / "agent-dirty"
    benign = real_repo / ".claude" / "worktrees" / "agent-benign"
    ahead = real_repo / ".claude" / "worktrees" / "agent-ahead"
    for path, branch in ((clean, "b1"), (dirty, "b2"), (benign, "b3"), (ahead, "b4")):
        _git(real_repo, "worktree", "add", "-b", branch, str(path))
    (dirty / "scratch.txt").write_text("uncommitted")
    (benign / ".last-cleanup").write_text("x")
    (ahead / "g.txt").write_text("y")
    _git(ahead, "add", "g.txt")
    _git(ahead, "commit", "-m", "ahead")

    result = _work.collect("day", repo_root=real_repo)
    directives = _by_id(result.directives)
    assert [d["id"] for d in result.directives if d["id"].startswith("d-worktree")] == [
        "d-worktree-reap-1",
        "d-worktree-reap-2",
        "d-worktree-reap-3",
    ]
    assert all(d["cli"] == "agent-worktree-sweep" and d["args"] == ["--reap"] for d in directives.values() if d["id"].startswith("d-worktree"))
    details = " ".join(d["detail"] for d in result.directives)
    assert "empty-clean" in details and "dirty-benign" in details and "commits-clean" in details
    (point,) = [p for p in result.judgment_points if p["id"].startswith("j-worktree-dirty")]
    assert point["id"] == "j-worktree-dirty-1" and "agent-dirty" in point["question"]
    assert [d["value"] for d in point["dispositions"]] == ["pm_reviews_manually", "leave_for_now"]


def test_req_w9_absent_without_agent_worktrees_or_an_active_branch(real_repo, tmp_path):
    assert [d for d in _work.collect("day", repo_root=real_repo).directives if d["id"].startswith("d-worktree")] == []
    _git(real_repo, "worktree", "add", "-b", "b9", str(real_repo / ".claude" / "worktrees" / "agent-x"))
    (real_repo / ".git" / "HEAD").write_text("0" * 40 + "\n")
    assert _work._worktree_entries(real_repo) == ([], [])
