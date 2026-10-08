"""phase1_checks: one halt per check, an all-pass case, and call order."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.plan_chain import phase1_checks as p1
from coordinator_core.ops.plan_chain.contract import ChainManifest, HALTS

MANIFEST = ChainManifest("s", "b", None, "pm", ".", "t", {}, "src")
PLAN = Path("plan.md")
VALID = {"verdict": "VALID", "rows": []}


@pytest.fixture
def calls(monkeypatch):
    log: list[str] = []

    def rec(name, value):
        def fn(*a, **k):
            log.append(name)
            return value

        return fn

    monkeypatch.setattr(p1, "_write_baseline", rec("baseline", None))
    monkeypatch.setattr(p1, "_check_plan", rec("spine", VALID))
    monkeypatch.setattr(p1, "_spine_read", rec("gates", (["row"], [])))
    monkeypatch.setattr(p1, "_falsifier_defect", rec("falsifier", None))
    monkeypatch.setattr(p1, "_plan_targets", rec("targets", ["a"]))
    return log


def _run():
    return p1.run(MANIFEST, PLAN, repo_root=Path("/repo"))


def test_all_pass_in_order(calls):
    assert _run() is None
    assert calls == ["baseline", "spine", "gates", "falsifier", "targets"]


def test_baseline_failure_halts(calls, monkeypatch):
    monkeypatch.setattr(p1, "_write_baseline", lambda *a: "plan-completeness no-spine: plan.md")
    h = _run()
    assert h.halted_at == HALTS["spine-check-failed"] and "no-spine" in h.reason
    assert calls == []


def test_spine_check_failed(calls, monkeypatch):
    monkeypatch.setattr(
        p1, "_check_plan", lambda *a: {"verdict": "INVALID", "rows": [{"at": "C1", "error": "bad"}]}
    )
    h = _run()
    assert h.halted_at == "execute" and "bad" in h.reason
    assert "gates" not in calls


def test_falsifier_finding_in_spine_report_defers_to_falsifier_owed(calls, monkeypatch):
    report = {"verdict": "INVALID", "rows": [{"at": p1._FALSIFIER_AT, "error": "x"}]}
    monkeypatch.setattr(p1, "_check_plan", lambda *a: report)
    monkeypatch.setattr(p1, "_falsifier_defect", lambda *a: "prime absent")
    h = _run()
    assert h.reason == "prime absent" and h.halted_at == "execute"


def test_external_gate_uncleared_names_row(calls, monkeypatch):
    monkeypatch.setattr(
        p1,
        "_spine_read",
        lambda *a: ([], [{"id": "C2", "reason": "deferred"}, {"id": "C7", "reason": "external_gate"}]),
    )
    h = _run()
    assert "C7" in h.reason and "C2" not in h.reason
    assert "falsifier" not in calls


def test_partly_gated_plan_runs_its_dispatchable_rows(calls, monkeypatch):
    monkeypatch.setattr(p1, "_spine_read", lambda *a: (["C1"], [{"id": "C7", "reason": "external_gate"}]))

    assert _run() is None
    assert "targets" in calls


def test_unreadable_spine_halts(calls, monkeypatch):
    def boom(*a):
        raise ValueError("dangling")

    monkeypatch.setattr(p1, "_spine_read", boom)
    assert "dangling" in _run().reason


def test_falsifier_owed(calls, monkeypatch):
    monkeypatch.setattr(p1, "_falsifier_defect", lambda *a: "falsifier absent")
    h = _run()
    assert h.reason == "falsifier absent"
    assert "targets" not in calls


def test_targets_refusal_halts(calls, monkeypatch):
    def boom(*a):
        raise ValueError("row(s) C1 declare no `writes:`")

    monkeypatch.setattr(p1, "_plan_targets", boom)
    h = _run()
    assert h.halted_at == "execute" and "writes" in h.reason


def test_no_subprocess_attribute_touched(calls, monkeypatch):
    def forbid(*a, **k):
        raise AssertionError("subprocess used")

    for name in ("run", "Popen", "check_output", "call"):
        monkeypatch.setattr(subprocess, name, forbid)
    assert _run() is None
    assert "subprocess" not in vars(p1)
