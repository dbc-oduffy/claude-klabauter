
from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops.session import reap

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _boom_reap_orphaned_claims(sessions_dir):
    raise AssertionError(
        "session.reap handler must never call _reap_orphaned_claims (C3) — "
        "sub-reap (iii) is ceremony-gate-only via session.reap_claims_for_repos"
    )


def test_gated_fast_path_never_calls_orphaned_claims_reaper(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True)
    marker = reap._last_reap_path(sessions_dir)
    marker.touch()

    monkeypatch.setattr(reap, "_reap_orphaned_claims", _boom_reap_orphaned_claims)

    result = asyncio.run(reap._handler({}, repo_root=sessions_dir.parent))

    assert result["cadence_gated"] is True
    assert result["reaped_claims"] == []
    assert result["reaped_sessions"] == []
    assert result["reaped_agents"] == []
    assert result["pruned_agent_archive"] == []
    assert result["failed"] == []
    assert result["deferred"] == []


def test_full_run_never_calls_orphaned_claims_reaper(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True)

    monkeypatch.setattr(reap, "_reap_orphaned_claims", _boom_reap_orphaned_claims)
    monkeypatch.setattr(reap, "resolve_live_session_ids", lambda: frozenset())
    monkeypatch.setattr(reap, "main_worktree_root", lambda common_dir: common_dir.parent)

    result = asyncio.run(reap._handler({"force": True}, repo_root=sessions_dir.parent))

    assert result["exit_code"] in (0, 2)
    assert result["reaped_claims"] == []
    assert result["cadence_gated"] is False
    assert reap._last_reap_path(sessions_dir).exists()


def test_claim_reap_still_reachable_via_reap_claims_for_repos(tmp_path, monkeypatch):
    calls = []

    def _fake_reap_orphaned_claims(sessions_dir):
        calls.append(sessions_dir)
        return [], [], []

    monkeypatch.setattr(reap, "_reap_orphaned_claims", _fake_reap_orphaned_claims)
    monkeypatch.setattr(reap, "git_common_dir", lambda target_root: target_root / ".git")

    target_root = tmp_path / "repo"
    (target_root / ".git" / "coordinator-sessions").mkdir(parents=True)

    result = asyncio.run(
        reap._handler_reap_claims_for_repos(
            {"target_roots": [str(target_root)]}, repo_root=None
        )
    )

    assert result["exit_code"] == 0
    assert len(calls) == 1
    assert calls[0] == target_root / ".git" / "coordinator-sessions"
