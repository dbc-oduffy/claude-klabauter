"""A refusal halt past the plan gate keeps its landed plan and execute phases on a refire of the sizing."""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, halted_run, kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

HALTED = "ask-20261001T000000-aaaaaa"
NEW_RUN = "ask-20261002T000000-bbbbbb"
GATE = {"plan": PLAN_REL, "plan_sha256": "0" * 64, "reviews": [], "coverage": None}


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _halt_refusal(repo, *, executed=True):
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("C1", "coded"), ("C2", "open")])
    record = repo / RECORD_DIR_ROOT / HALTED
    halted_run.write_halt_marker(record, halted="refusal", sizing_rel=REL, plan_rel=PLAN_REL)
    chunks = {}
    if executed:
        target = repo / "some" / "C2.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"print('c2')\n")
        chunks = kept_work.kept_chunks(repo, {"some/C2.py": "C2"}, {"some/C2.py": "halt-record-only"})
    kept_work.write(record, chunks, GATE)


def _refire(repo):
    return ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=repo / ".git")


def _minted(repo):
    return sorted(os.listdir(repo / RECORD_DIR_ROOT))


def test_refusal_after_execute_stays_bound_and_credits_plan_and_execute(repo, monkeypatch):  # noqa: F811
    _halt_refusal(repo)
    monkeypatch.setattr(ask_gate, "_plan_gate_resume_ready", lambda *a: True)
    assert halted_run.halted_run_to_resume(repo, REL) == HALTED
    before = _minted(repo)
    phases = _refire(repo)["resume_phases"]
    assert set(phases["reuse"]) >= {"plan", "plan_review", "plan_gate_rulings", "execute"}
    assert phases["rerun"][0] == "gate" and phases["rerun"][-4:] == ["review", "test", "criterion", "commit"]
    assert [n for n in _minted(repo) if n not in before and n != NEW_RUN] == []


def test_refusal_at_the_plan_gate_still_reruns_execute(repo, monkeypatch):  # noqa: F811
    _halt_refusal(repo, executed=False)
    monkeypatch.setattr(ask_gate, "_plan_gate_resume_ready", lambda *a: True)
    phases = _refire(repo)["resume_phases"]
    assert "execute" not in phases["reuse"]
    assert "plan_gate_rulings" in phases["reuse"] or "plan_gate_rulings" in phases["rerun"]


def test_refusal_after_execute_credits_the_plan_without_the_readiness_check(repo, monkeypatch):  # noqa: F811
    _halt_refusal(repo)
    monkeypatch.setattr(ask_gate, "_plan_gate_resume_ready", lambda *a: False)
    phases = _refire(repo)["resume_phases"]
    assert {"plan", "plan_review", "execute"} <= set(phases["reuse"])
