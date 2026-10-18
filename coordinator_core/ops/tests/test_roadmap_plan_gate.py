"""Tests for the roadmap.plan_gate op wrapper over gate_v2."""

from __future__ import annotations

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import roadmap_plan_gate as op_module
from coordinator_core.roadmap import gate_v2


def _gate(open_: bool, blocking=()):
    return {"open": open_, "blocking": list(blocking)}


def _report():
    return {
        "batons": [
            {
                "id": "b-1",
                "path": "state/handoffs/b-1.md",
                "stub_id": "stub-1",
                "planning_gate": _gate(True),
                "execution_gate": _gate(False, [{"blocker": "x-1"}]),
            },
            {
                "id": "b-2",
                "path": "state/handoffs/b-2.md",
                "stub_id": None,
                "planning_gate": _gate(True),
                "execution_gate": _gate(True),
            },
        ],
        "waves": [], "cycles": [], "unresolved_blockers": [], "untracked": [],
        "held": [], "counts": {}, "unmatched_targets": [], "verdict": None,
    }


@pytest.fixture()
def seen(monkeypatch, tmp_path):
    calls = []

    def fake(root, **kwargs):
        calls.append((root, kwargs))
        return _report()

    monkeypatch.setattr(gate_v2, "compute_gate_report", fake)
    monkeypatch.setattr(op_module, "main_worktree_root", lambda r: r)
    return calls


def _call(tmp_path, **params):
    return op_module._handler(params, repo_root=tmp_path)


def test_the_op_is_registered_under_its_wire_name():
    from coordinator_core.ipc import _REGISTRY

    assert "roadmap.plan_gate" in _REGISTRY


def test_the_op_is_reachable_through_the_lazy_import_map():
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP["roadmap.plan_gate"] == "coordinator_core.ops.roadmap_plan_gate"


def test_the_op_declares_a_key_scope():
    from coordinator_core.op_scopes import _OP_KEY_SCOPE

    assert _OP_KEY_SCOPE["roadmap.plan_gate"] == "common_dir"


def test_selectors_pass_through(seen, tmp_path):
    _call(tmp_path, roadmap_id="rm-1", targets=["b-1"], subject="b-1", gate="planning")
    root, kwargs = seen[0]
    assert root == tmp_path
    assert kwargs == {"roadmap_id": "rm-1", "targets": ["b-1"], "subject": "b-1"}


def test_no_selectors_sweeps(seen, tmp_path):
    _call(tmp_path)
    assert seen[0][1] == {"roadmap_id": None, "targets": None, "subject": None}


def test_no_subject_leaves_verdict_null(seen, tmp_path):
    assert _call(tmp_path)["verdict"] is None


def test_subject_with_gate_returns_holding_blockers(seen, tmp_path):
    v = _call(tmp_path, subject="stub-1", gate="execution")["verdict"]
    assert v == {
        "subject": "stub-1", "resolved": True, "gate": "execution",
        "open": False, "blocking": [{"blocker": "x-1"}],
    }


def test_subject_default_gate_carries_both(seen, tmp_path):
    v = _call(tmp_path, subject="b-2")["verdict"]
    assert v["resolved"] is True
    assert v["planning_gate"]["open"] and v["execution_gate"]["open"]


def test_both_gates_open_requires_execution_gate_open(seen, tmp_path):
    v = _call(tmp_path, subject="b-1")["verdict"]
    assert v["planning_gate"]["open"] and not v["execution_gate"]["open"]
    assert v["open"] is False


def test_unknown_subject_is_unresolved_and_shut(seen, tmp_path):
    v = _call(tmp_path, subject="nope")["verdict"]
    assert v["resolved"] is False and v["open"] is False


@pytest.mark.parametrize("params", [
    {"gate": "bogus"}, {"targets": []}, {"targets": "b-1"}, {"subject": 3}, {"roadmap_id": 3},
])
def test_bad_params_are_rejected(seen, tmp_path, params):
    with pytest.raises(ValueError):
        _call(tmp_path, **params)
    assert seen == []


def test_missing_repo_root_is_rejected():
    with pytest.raises(ValueError):
        op_module._handler({}, repo_root=None)


def test_refused_read_arrives_as_an_error_reply_not_a_report(tmp_path, monkeypatch):
    def refuse(root, **kwargs):
        raise gate_v2.GateReadRefused("handoff dir unreadable")

    monkeypatch.setattr(gate_v2, "compute_gate_report", refuse)
    monkeypatch.setattr(op_module, "main_worktree_root", lambda r: r)
    with pytest.raises(CallerFacingValidationError, match="handoff dir unreadable"):
        _call(tmp_path)
