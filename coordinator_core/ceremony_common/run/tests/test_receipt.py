"""Shared receipt renderer: record sections, spec-driven render, narration check."""
from __future__ import annotations

import pytest

from coordinator_core.ceremony_common.run import receipt
from coordinator_core.ceremony_common.run.contract import NARRATION_DENY_LIST, Decision, StepResult, StepSpec


class _Spec:
    name = "demo"

    def __init__(self, sections):
        self.receipt_sections = sections


def _step(sid, stage="s", cost="engine", args=(), gates=False):
    return StepSpec(sid, stage, "cli", sid, tuple(args), None, False, "", "### X", cost, gates_next=gates)


def _dec(i, by="rule", rule="r", decision="leave", acted=()):
    return Decision(f"p{i}", f"q{i}?", decision, by, rule, f"why{i}", "ev", tuple(acted))


def _ran(sid, out="", ms=None, spawns=0):
    return StepResult(sid, "ran", stdout=out, process_ms=ms, spawns=spawns)


def test_agent_decision_has_decider_rationale_and_acted():
    rec = receipt.render_record([], [_dec(1, by="agent", acted=("did-x",))], [], expected_step_ids=[])
    assert "(agent): why1; acted: did-x" in rec["Decided"]


def test_forty_rule_decisions_group_to_one_line():
    rec = receipt.render_record([], [_dec(i) for i in range(40)], [], expected_step_ids=[])
    lines = [ln for ln in rec["Decided"].splitlines() if "rule `r`" in ln]
    assert lines == ["- rule `r` x40: leave; p0, p1, p2, ..."]


def test_failed_row_has_backlog_and_no_command():
    steps = {"a": _step("a"), "bl": _step("bl", stage="failure-backlog", args=("a",))}
    rows = [StepResult("a", "failed", reason="boom"), _ran("bl", "BL-7\n")]
    rec = receipt.render_record(rows, [], [], expected_step_ids=["a", "bl"], steps=steps)
    assert rec["Failed"] == "- `a`: boom; backlog row: BL-7"


def test_false_condition_section_absent():
    spec = _Spec((("### Alpha", "always", "step:a"), ("### Beta", "has_beta", "step:a")))
    md = receipt.render(spec, [_ran("a", "line")], [], [], None, None, facts={})
    assert "### Alpha" in md and "### Beta" not in md


def test_deny_phrase_in_step_output_is_data_but_in_heading_raises():
    phrase = NARRATION_DENY_LIST[0]
    md = receipt.render(_Spec((("### Alpha", "always", "step:a"),)), [_ran("a", phrase)], [], [], None, None, facts={})
    assert phrase in md
    with pytest.raises(ValueError):
        receipt.render(_Spec(((f"### {phrase}", "always", "step:a"),)), [_ran("a", "x")], [], [], None, None, facts={})


def test_expected_step_absent_from_ledger_raises():
    with pytest.raises(ValueError, match="no ledger row"):
        receipt.render_record([], [], [], expected_step_ids=["gone"])


def test_record_heading_in_spec_sections_raises():
    with pytest.raises(ValueError):
        receipt.render(_Spec((("### Failed", "always", "step:a"),)), [_ran("a", "x")], [], [], None, None, facts={})


def test_suite_and_remote_cost_get_own_lines_and_record_positions_fixed():
    steps = {"a": _step("a"), "t": _step("t", cost="suite"), "r": _step("r", cost="remote")}
    rows = [_ran("a", "x", 10), _ran("t", "", 900), _ran("r", "", 100)]
    spec = _Spec((("### Alpha", "always", "step:a"),))
    md = receipt.render(spec, rows, [], ["seen"], None, None, facts={"steps": steps})
    assert "- suite: 1 steps, 900ms" in md and "- remote: 1 steps, 100ms" in md
    heads = [ln for ln in md.splitlines() if ln.startswith("### ")]
    assert heads == ["### Alpha", "### Observations", "### Run cost"]
    assert "Parked for you" not in md
