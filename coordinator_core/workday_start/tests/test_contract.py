"""Pins the runner contract against its schema file."""
from __future__ import annotations

import copy

from coordinator_core.contract.decision_object.reader_result import build_directive
from coordinator_core.workday_start import contract as c


def _digest() -> dict:
    return c.build_digest(
        ceremony="workday-start",
        run_id="20261012T093000Z-ab12",
        repo_root="/repo",
        state="ran",
        finish_argv=["finish", "--run-id", "20261012T093000Z-ab12"],
        groups=[
            {
                "id": "inbox", "phase": 1, "agent": "coordinator:executor",
                "ruling_agent": "coordinator:apm", "inputs": {"supersession_candidates": []},
                "entries": [{"key": "m1", "brief": "triage", "argv": None, "inputs": {"memos": ["a.md"]}}],
            },
            {
                "id": "backfill", "phase": 2, "agent": None, "ruling_agent": None, "inputs": {},
                "entries": [{"key": "2026-10-10", "brief": None, "argv": ["x"], "inputs": {"kind": "day"}}],
            },
        ],
    )


def _results() -> dict:
    return {"groups": {"inbox": [{
        "key": "m1", "outcome": "needs-ruling", "commit": None,
        "decision": {"disposition": "accept", "rationale": "because"},
        "question": "q", "options": ["accept", "decline"], "memos": ["a.md"],
    }]}}


def test_digest_validates():
    assert c.validate_digest(_digest()) == []


def test_digest_rejects_bad_state_and_extra_key():
    bad = _digest()
    bad["state"] = "waiting"
    assert c.validate_digest(bad)
    bad = _digest()
    bad["surprise"] = 1
    assert c.validate_digest(bad)


def test_digest_group_phase_is_integer():
    bad = copy.deepcopy(_digest())
    bad["agent_work"]["groups"][0]["phase"] = "one"
    assert c.validate_digest(bad)


def test_build_digest_raises_on_invalid():
    try:
        c.build_digest(ceremony="x", run_id="nope", repo_root="/r", state="ran", finish_argv=[], groups=[])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_agent_results_validate():
    assert c.validate_agent_results(_results()) == []


def test_no_handback_disposition_validates():
    assert c.HANDBACK_DISPOSITIONS
    for value in c.HANDBACK_DISPOSITIONS:
        obj = _results()
        obj["groups"]["inbox"][0]["decision"]["disposition"] = value
        assert c.validate_agent_results(obj), value


def test_stage_tuples_disjoint_and_unique():
    assert not set(c.LAUNCH_STAGES) & set(c.FINISH_STAGES)
    assert len(set(c.LAUNCH_STAGES)) == len(c.LAUNCH_STAGES)
    assert len(set(c.FINISH_STAGES)) == len(c.FINISH_STAGES)


def test_step_from_directive_round_trips_build_directive():
    d = build_directive("d1", "some-cli", ["--a", "b"], "why")
    d["stdin_from"] = "ignored"
    step = c.step_from_directive(d, stage="orient", heading="H", cost_class="engine")
    assert (step.id, step.target, step.args, step.depends_on, step.already_satisfied, step.detail) == (
        "d1", "some-cli", ("--a", "b"), None, False, "why")
    assert step.kind == "cli" and step.stage == "orient" and not step.gates_next and step.on_exit == {}


def test_on_exit_rules_are_known_and_deny_list_is_closed():
    assert set(c.RULE_IDS) >= {"step0-stale-branch", "step0-reconcile-outcome"}
    assert "Parked for you" in c.NARRATION_DENY_LIST
    assert c.EXIT_GATE_FAILED == 5 and c.EXIT_IN_FLIGHT == 3


def test_ledger_row_and_decision_rows():
    row = c.StepResult(id="s", status="ran").to_row()
    assert tuple(row) == c.LEDGER_ROW_KEYS
    d = c.Decision("p", "q", "defer", "rule", "recommendation", "r", "e", ("s",)).to_row()
    assert d["acted"] == ["s"]
