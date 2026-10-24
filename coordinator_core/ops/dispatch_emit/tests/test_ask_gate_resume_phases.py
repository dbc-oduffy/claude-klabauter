"""dispatch.ask_gate replies `resume_phases` on a resume of a halted run, and only there."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, halted_run, kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

RUN = "ask-20261001T000000-aaaaaa"
NEW_RUN = "ask-20261002T000000-bbbbbb"


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _halt(repo, kind, *, plan_gate=None):
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("C1", "coded"), ("C2", "open"), ("C3", "open")])
    record = repo / RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(record, halted=kind, sizing_rel=REL, plan_rel=PLAN_REL)
    target = repo / "some" / "C2.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"print('c2')\n")
    kept_work.write(
        record, kept_work.kept_chunks(repo, {"some/C2.py": "C2"}, {"some/C2.py": "halt-record-only"}), plan_gate
    )
    return record


def _reply(repo):
    return ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=repo / ".git")


def test_review_blocked_resume_reuses_execute_and_reruns_review_and_dependents(repo):  # noqa: F811
    _halt(repo, "review_blocked")
    phases = _reply(repo)["resume_phases"]
    assert phases["reuse"] == ["execute"]
    assert phases["rerun"] == ["gate", "size", "plan", "plan_review", "plan_gate_rulings", "review", "test", "criterion", "commit"]
    assert list(phases["verdicts"]) == ["execute"]


def test_unknown_halt_kind_reuses_nothing(repo):  # noqa: F811
    _halt(repo, "some-unmapped-kind")
    phases = _reply(repo)["resume_phases"]
    assert phases["reuse"] == [] and phases["verdicts"] == {}


def test_a_fresh_run_carries_no_resume_phases(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    assert "resume_phases" not in _reply(repo)


def test_plan_credit_rides_only_the_plan_gate_ready_check(repo, monkeypatch):  # noqa: F811
    gate_record = {"plan": PLAN_REL, "plan_sha256": "0" * 64, "reviews": [], "coverage": None}
    _halt(repo, "review_blocked", plan_gate=gate_record)
    monkeypatch.setattr(ask_gate, "_plan_gate_resume_ready", lambda *a: False)
    refused = _reply(repo)["resume_phases"]
    assert "plan" not in refused["reuse"] and "plan_review" not in refused["reuse"]
    monkeypatch.setattr(ask_gate, "_plan_gate_resume_ready", lambda *a: True)
    credited = _reply(repo)["resume_phases"]
    assert {"plan", "plan_review", "execute"} <= set(credited["reuse"])
    assert credited["verdicts"]["plan"]["plan"] == PLAN_REL


def test_a_revise_note_invalidates_criterion_and_commit_only(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import phase_ledger

    landed = {"plan": {}, "plan_review": {}, "execute": {}}
    phases = phase_ledger.resume_phases(landed, None, ["requirement"])
    assert phases["reuse"] == ["plan", "plan_review", "execute"]
    assert {"criterion", "commit"} <= set(phases["rerun"])
