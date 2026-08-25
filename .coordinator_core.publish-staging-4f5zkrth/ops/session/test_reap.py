"""
Tests for the C3 boot-backstop cull removal on `coordinator_core.ops.session.reap`
(session.reap handler): sub-reap (iii) — orphaned claim dirs, the one
NOT-git-reversible rm -rf among session.reap's four sub-reaps — must never run
from the `session.reap` op handler, on either the gated fast path or the full
run. It remains reachable only via the already-registered, already
target-root-parameterized `session.reap_claims_for_repos` op, which a ceremony
gate (/workday-start, /workday-complete, /consolidate-git, /merge-to-main)
invokes directly.

Spec backlink: dispatch brief C3, "Passengers leave boot for the ceremony
gates; the irreversible cull goes with them" —
docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md, AC6 / AC6d.

Negative-spec: does not re-exercise sub-reaps (i)/(ii)/(iv) behavior (already
covered by coordinator_core/ops/session/tests/test_reap.py) — pure coverage of
the C3 removal: `_reap_orphaned_claims` is never called by `_handler` (either
path), and the wire envelope's `reaped_claims` stays `[]` always.
"""

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
    """< 12h since last reap (gated, force=False): the handler must return a
    no-op envelope and must NOT call `_reap_orphaned_claims` at all — the old
    Decision D1 shape (a) fast path that ran sub-reap (iii) unconditionally on
    this branch is gone."""
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True)
    marker = reap._last_reap_path(sessions_dir)
    marker.touch()  # cadence just reset — well within the 12h window

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
    """force=True (full run, cadence gate bypassed): (i)/(ii)/(iv) may run, but
    `_reap_orphaned_claims` must never be called — sub-reap (iii) is fully
    extracted from session.reap, not merely re-gated."""
    sessions_dir = tmp_path / "coordinator-sessions"
    sessions_dir.mkdir(parents=True)

    monkeypatch.setattr(reap, "_reap_orphaned_claims", _boom_reap_orphaned_claims)
    monkeypatch.setattr(reap, "resolve_live_session_ids", lambda: frozenset())
    monkeypatch.setattr(reap, "main_worktree_root", lambda common_dir: common_dir.parent)

    result = asyncio.run(reap._handler({"force": True}, repo_root=sessions_dir.parent))

    assert result["exit_code"] in (0, 2)
    assert result["reaped_claims"] == []
    assert result["cadence_gated"] is False
    # .last-reap marker touched on the full-run path (unaffected by the removal).
    assert reap._last_reap_path(sessions_dir).exists()


def test_claim_reap_still_reachable_via_reap_claims_for_repos(tmp_path, monkeypatch):
    """The passenger is relocated, not deleted: `session.reap_claims_for_repos`
    (the pre-existing, already-registered per-repo primitive) still reaches
    `_reap_orphaned_claims` for a caller-named target_root — this is the
    ceremony-gate invocation path C3 leaves in place."""
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
