"""Replay guard: a workstream-complete close with an advisory gate is SUCCESS; a halting gate is not.

Observation is the apply ladder's exit code (labelled via `apply_halt.exit_code_label`) plus
landed/failed/degraded counted off the report. Dispatch and the repo-identity/verdict-memo
seams are stubbed, so nothing spawns.
"""

from __future__ import annotations

from typing import Any

import pytest

from coordinator_core.ceremony_common.apply_halt import exit_code_label
from coordinator_core.tests.gate_verdict.contract import (
    GateExpectation,
    GateObservation,
    VerdictClass,
    disagreement,
)
from coordinator_core.workstream_complete import apply as wsc_apply

GATE_VERDICT_CASE = "workstream-complete-apply"

_GATE = "review-brightline-gate"
_GATE_STDERR = "brightline gate: partition mandatory"


def _directive(did: str, cli: str, **extra: Any) -> dict[str, Any]:
    return {"id": did, "cli": cli, "args": [], **extra}


def _observe(monkeypatch, tmp_path, directives, results):
    def _fake_dispatch(directive, args=None):
        return dict(results[directive["id"]])

    monkeypatch.setattr(wsc_apply, "_dispatch_directive", _fake_dispatch)
    monkeypatch.setattr(
        wsc_apply,
        "compute_repo_identity_gate",
        lambda root, sid: {"verdict": "MATCH", "message": ""},
    )
    monkeypatch.setattr(
        wsc_apply.directives_review, "record_gate_verdict_if_passed", lambda *a, **k: None
    )
    exit_code, report = wsc_apply._execute_directives(
        directives, [], {}, repo_root=tmp_path, sid="sid-gate-verdict"
    )
    observation = GateObservation(
        exit_code=exit_code,
        verdict=exit_code_label(exit_code, report),
        magnitude={
            "landed": len(report["landed"]),
            "failed": len(report["failed"]),
            "degraded": len(report["degraded"]),
        },
    )
    return observation, report


def _ok(stdout: str = "") -> dict[str, Any]:
    return {"exit_code": 0, "stdout": stdout, "stderr": "", "args": []}


def _fail() -> dict[str, Any]:
    return {"exit_code": 1, "stdout": "", "stderr": _GATE_STDERR, "args": []}


def test_known_clean_close_with_advisory_gate_is_success(monkeypatch, tmp_path):
    directives = [
        _directive("d-a", "coordinator-complete-entry"),
        _directive("d-gate", _GATE),
        _directive("d-b", "regenerate-orientation-cache"),
    ]
    results = {
        "d-a": _ok("entry.md\n"),
        "d-gate": _ok("VERDICT=PARTITION-MANDATORY\n"),
        "d-b": _ok(),
    }
    observed, _ = _observe(monkeypatch, tmp_path, directives, results)
    expected = GateExpectation(
        "SUCCESS", VerdictClass.ADVISORY, {"landed": 3, "failed": 0, "degraded": 0}
    )
    assert disagreement(GATE_VERDICT_CASE, observed, expected) is None


def test_halting_gate_with_a_landed_sibling_is_partial_mutation(monkeypatch, tmp_path):
    directives = [
        _directive("d-a", "regenerate-orientation-cache"),
        _directive("d-gate", _GATE),
    ]
    results = {"d-a": _ok(), "d-gate": _fail()}
    observed, report = _observe(monkeypatch, tmp_path, directives, results)
    expected = GateExpectation(
        "PARTIAL_MUTATION", VerdictClass.HALT, {"landed": 1, "failed": 1, "degraded": 0}
    )
    assert disagreement(GATE_VERDICT_CASE, observed, expected) is None
    assert _GATE_STDERR in report["failed"][0]["error"]


def test_halting_gate_alone_is_directive_failed(monkeypatch, tmp_path):
    observed, report = _observe(
        monkeypatch, tmp_path, [_directive("d-gate", _GATE)], {"d-gate": _fail()}
    )
    expected = GateExpectation(
        "DIRECTIVE_FAILED", VerdictClass.HALT, {"landed": 0, "failed": 1, "degraded": 0}
    )
    assert disagreement(GATE_VERDICT_CASE, observed, expected) is None
    assert _GATE_STDERR in report["failed"][0]["error"]


def test_best_effort_gate_failure_is_degraded_success(monkeypatch, tmp_path):
    observed, _ = _observe(
        monkeypatch,
        tmp_path,
        [_directive("d-gate", _GATE, best_effort=True)],
        {"d-gate": _fail()},
    )
    expected = GateExpectation(
        "SUCCESS", VerdictClass.ADVISORY, {"landed": 0, "failed": 0, "degraded": 1}
    )
    assert disagreement(GATE_VERDICT_CASE, observed, expected) is None


@pytest.mark.parametrize("verdict_class", [VerdictClass.CLEAN, VerdictClass.HALT])
def test_harness_refuses_a_wrong_expectation(monkeypatch, tmp_path, verdict_class):
    observed, _ = _observe(
        monkeypatch, tmp_path, [_directive("d-gate", _GATE)], {"d-gate": _fail()}
    )
    wrong = GateExpectation("SUCCESS", verdict_class, {"landed": 1})
    assert disagreement(GATE_VERDICT_CASE, observed, wrong) is not None
