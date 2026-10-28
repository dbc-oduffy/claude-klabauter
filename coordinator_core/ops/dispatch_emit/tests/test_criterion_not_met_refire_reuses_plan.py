"""A criterion_not_met halt maps to execute: its refire reuses plan/plan_review and reruns execute on."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, halted_run, kept_work, phase_ledger
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

RUN = "ask-20261001T000000-aaaaaa"
NEW_RUN = "ask-20261002T000000-bbbbbb"
GATE = {"plan": PLAN_REL, "plan_sha256": "0" * 64, "reviews": [], "coverage": None}


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _halt(repo, kind):
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("C1", "coded"), ("C2", "open")])
    record = repo / RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted=kind, sizing_rel=REL, plan_rel=PLAN_REL)
    target = repo / "some" / "C2.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"print('c2')\n")
    kept_work.write(
        record, kept_work.kept_chunks(repo, {"some/C2.py": "C2"}, {"some/C2.py": "halt-record-only"}), GATE
    )


def _reply(repo):
    return ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=repo / ".git")["resume_phases"]


def test_halt_phase_maps_criterion_not_met_to_execute():
    assert phase_ledger.HALT_PHASE["criterion_not_met"] == "execute"


def test_ledger_reuses_plan_and_reruns_execute_onward():
    landed = {"plan": {}, "plan_review": {}, "execute": {}}
    phases = phase_ledger.resume_phases(landed, "criterion_not_met")
    assert phases["reuse"] == ["plan", "plan_review"]
    assert phases["rerun"][phases["rerun"].index("execute"):] == ["execute", "review", "test", "criterion", "commit"]


def test_gate_reuses_plan_even_when_plan_gate_readiness_is_false(repo, monkeypatch):  # noqa: F811
    _halt(repo, "criterion_not_met")
    monkeypatch.setattr(ask_gate, "_plan_gate_resume_ready", lambda *a: False)
    phases = _reply(repo)
    assert {"plan", "plan_review"} <= set(phases["reuse"])
    assert "execute" in phases["rerun"]


def test_plan_gate_halt_still_pops_plan_when_not_ready(repo, monkeypatch):  # noqa: F811
    _halt(repo, "plan_gate")
    monkeypatch.setattr(ask_gate, "_plan_gate_resume_ready", lambda *a: False)
    phases = _reply(repo)
    assert "plan" not in phases["reuse"] and "plan_review" not in phases["reuse"]
