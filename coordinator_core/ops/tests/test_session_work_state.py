"""
coordinator_core.ops.tests.test_session_work_state — JSON-RPC veneer tests
for "session.work_state".

Spec backlink: docs/plans/2026-08-19-fleet-work-state-who-holds-which-baton.md,
chunk C3
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.ops.session_work_state as sws_op
from coordinator_core.authz.registration_quad import check_registration_quad


def test_veneer_converts_injected_common_dir_to_the_worktree_root(monkeypatch, tmp_path):
    """The op's scope is "common_dir", so the engine injects `<worktree>/.git`,
    NOT the worktree root. `build_work_state` scans `<root>/state/handoffs`, so
    passing the injected value straight through makes this op return an empty
    readout for every repo — which is exactly how it shipped, and what no test
    caught, because every other test here stubs `build_work_state` or calls it
    directly with a tmp_path. Pin the conversion, not the pass-through."""
    captured = {}

    def _fake_build_work_state(repo_root):
        captured["repo_root"] = repo_root
        return {"held": [], "unclaimed": [], "review_due": []}

    monkeypatch.setattr(sws_op, "build_work_state", _fake_build_work_state)

    (tmp_path / ".git").mkdir()
    sws_op._session_work_state({}, repo_root=tmp_path / ".git")

    assert captured["repo_root"] == tmp_path


def test_absent_repo_root_is_a_well_formed_empty_answer_never_a_raise():
    """Mirrors handoff.columns / records.query: absent repo_root degrades to an
    empty payload carrying all three buckets, rather than raising."""
    assert sws_op._session_work_state({}, repo_root=None) == {
        "held": [],
        "unclaimed": [],
        "review_due": [],
    }


def test_veneer_ignores_params_repo_root(monkeypatch, tmp_path):
    """Unlike session.peer_roster, this op takes repo_root ONLY as the
    engine-injected kwarg -- a wire-level params["repo_root"] must be
    ignored, never used as an override."""
    captured = {}

    def _fake_build_work_state(repo_root):
        captured["repo_root"] = repo_root
        return {"held": [], "unclaimed": []}

    monkeypatch.setattr(sws_op, "build_work_state", _fake_build_work_state)

    other = Path("/somewhere/else")
    (tmp_path / ".git").mkdir()
    sws_op._session_work_state({"repo_root": str(other)}, repo_root=tmp_path / ".git")

    assert captured["repo_root"] == tmp_path


def test_veneer_returns_verbatim_shape(monkeypatch, tmp_path):
    payload = {"held": [{"path": "x"}], "unclaimed": [{"path": "y"}], "review_due": []}
    monkeypatch.setattr(sws_op, "build_work_state", lambda repo_root: payload)

    (tmp_path / ".git").mkdir()
    result = sws_op._session_work_state({}, repo_root=tmp_path / ".git")

    assert result is payload


def test_registration_quad_clean_for_session_work_state():
    """AC5: session.work_state carries all five registration surfaces --
    OP_CLASSIFICATION, op_scopes.OP_KEY_SCOPE, OP_MODULE_MAP, and
    _EAGER_OP_MODULES membership, in addition to the live @register_op
    self-registration this import performs."""
    violations = check_registration_quad()
    op_keys_with_violations = {v.op_key for v in violations}
    assert "session.work_state" not in op_keys_with_violations
