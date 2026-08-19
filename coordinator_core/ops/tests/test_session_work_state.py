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


def test_veneer_calls_build_work_state_with_engine_repo_root(monkeypatch, tmp_path):
    captured = {}

    def _fake_build_work_state(repo_root):
        captured["repo_root"] = repo_root
        return {"held": [], "unclaimed": []}

    monkeypatch.setattr(sws_op, "build_work_state", _fake_build_work_state)

    result = sws_op._session_work_state({}, repo_root=tmp_path)

    assert captured["repo_root"] == tmp_path
    assert result == {"held": [], "unclaimed": []}


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
    sws_op._session_work_state({"repo_root": str(other)}, repo_root=tmp_path)

    assert captured["repo_root"] == tmp_path


def test_veneer_returns_verbatim_shape(monkeypatch, tmp_path):
    payload = {"held": [{"path": "x"}], "unclaimed": [{"path": "y"}]}
    monkeypatch.setattr(sws_op, "build_work_state", lambda repo_root: payload)

    result = sws_op._session_work_state({}, repo_root=tmp_path)

    assert result is payload


def test_registration_quad_clean_for_session_work_state():
    """AC5: session.work_state carries all five registration surfaces --
    OP_CLASSIFICATION, op_scopes.OP_KEY_SCOPE, OP_MODULE_MAP, and
    _EAGER_OP_MODULES membership, in addition to the live @register_op
    self-registration this import performs."""
    violations = check_registration_quad()
    op_keys_with_violations = {v.op_key for v in violations}
    assert "session.work_state" not in op_keys_with_violations
