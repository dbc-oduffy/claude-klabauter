
"""Unit tests for `coordinator_core.ops.session.reap`: pins that session.reap never culls claim dirs and that the deleted claim-reap surface stays deleted."""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops.session import reap

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_claim_reap_surface_is_deleted():
    for name in (
        "_reap_orphaned_claims",
        "_reap_claims_for_target",
        "_reap_session_touched_repos",
        "_handler_reap_claims_for_repos",
    ):
        assert not hasattr(reap, name), name


def test_gated_fast_path_reaps_no_claims(tmp_path):
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True)
    marker = reap._last_reap_path(sessions_dir)
    marker.touch()

    result = asyncio.run(reap._handler({}, repo_root=sessions_dir.parent))

    assert result["cadence_gated"] is True
    assert result["reaped_claims"] == []
    assert result["reaped_sessions"] == []
    assert result["reaped_agents"] == []
    assert result["pruned_agent_archive"] == []
    assert result["failed"] == []
    assert result["deferred"] == []


def test_full_run_reaps_no_claims(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True)

    monkeypatch.setattr(reap, "resolve_live_session_ids", lambda: frozenset())
    monkeypatch.setattr(reap, "main_worktree_root", lambda common_dir: common_dir.parent)

    result = asyncio.run(reap._handler({"force": True}, repo_root=sessions_dir.parent))

    assert result["exit_code"] in (0, 2)
    assert result["reaped_claims"] == []
    assert result["cadence_gated"] is False
    assert reap._last_reap_path(sessions_dir).exists()
