"""Briefing renderer: record-only output, closed section set, narration deny list."""
from __future__ import annotations

import pytest

from coordinator_core.workday_start.briefing import SECTION_ORDER, render
from coordinator_core.workday_start.contract import NARRATION_DENY_LIST, Decision, StepResult, StepSpec


def _spec(sid, heading="### Orphan Sweep", stage="sentinels", cost="engine", gates=False, args=()):
    return StepSpec(sid, stage, "cli", sid, tuple(args), None, False, "", heading, cost, gates_next=gates)


def _dec(i, by="rule", rule="recommendation", decision="leave"):
    return Decision(f"p{i}", f"q{i}?", decision, by, rule, f"why{i}", "ev", ())


def _render(rows, steps, decisions=(), obs=(), envelope=None):
    return render(rows, list(decisions), list(obs), envelope, date="2026-10-12", branch="main", steps=steps)


def _headings(md):
    return [ln for ln in md.splitlines() if ln.startswith("### ")]


def test_agent_decision_has_decider_and_rationale():
    d = Decision("p1", "Merge branch?", "merge", "agent:coordinator:apm", "", "tests green", "ev", ("step-a",))
    md = _render([], [], [d])
    assert "Merge branch? -> merge (agent:coordinator:apm): tests green; acted: step-a" in md
    assert "decisions.jsonl" in md.splitlines()[-1]


def test_forty_rule_decisions_group_to_one_line():
    md = _render([], [], [_dec(i) for i in range(40)])
    lines = [ln for ln in md.splitlines() if "rule `recommendation`" in ln]
    assert len(lines) == 1 and "x40" in lines[0] and "p0, p1, p2, ..." in lines[0]


def test_step0_rule_decision_is_own_line_and_first():
    ds = [_dec(1), _dec(2, rule="step0-stale-branch", decision="rebase")]
    lines = _render([], [], ds).splitlines()
    idx = [i for i, ln in enumerate(lines) if "step0-stale-branch" in ln]
    assert len(idx) == 1 and idx[0] < [i for i, ln in enumerate(lines) if "`recommendation`" in ln][0]


def test_failed_row_has_backlog_and_no_command():
    steps = [_spec("s1", gates=True), _spec("s2"), _spec("fb", stage="failure-backlog", args=("s1",))]
    rows = [
        StepResult("s1", "failed", reason="exit 1"),
        StepResult("s2", "halted-by-gate", reason="s1"),
        StepResult("fb", "ran", stdout="BUG-42\n"),
    ]
    md = _render(rows, steps)
    assert "### Failed" in md
    assert "`s1`: exit 1; backlog row: BUG-42" in md
    assert "- halted-by-gate: 1" in md
    assert "rerun" not in md.lower()


def test_no_parked_heading_and_closed_section_set():
    steps = [_spec("s1")]
    rows = [StepResult("s1", "failed", reason="x")]
    envelope = {"judgment_points": [{"id": "j1"}], "next_move": "go on"}
    md = _render(rows, steps, [_dec(1)], ["step0 deferred"], envelope)
    assert "Parked for you" not in md
    assert all(h[4:] in SECTION_ORDER for h in _headings(md))
    assert 'envelope output: "go on"' in md


def test_deny_listed_strings_only_as_quoted_data():
    steps = [_spec("s1")]
    rows = [StepResult("s1", "ran", stdout="TODO: fix lint\n")]
    md = _render(rows, steps, envelope={"next_move": "Next up: ship"})
    assert "Next up" not in md
    authored = [ln for ln in md.splitlines() if not ln.startswith("TODO")]
    for phrase in NARRATION_DENY_LIST:
        assert not any(phrase.lower() in ln.lower() for ln in authored)
    assert "TODO: fix lint" in md


def test_deny_listed_heading_raises():
    steps = [_spec("s1", heading="### Parked for you")]
    rows = [StepResult("s1", "ran", stdout="x")]
    with pytest.raises(ValueError, match="deny-listed"):
        _render(rows, steps)


def test_heading_outside_set_raises():
    with pytest.raises(ValueError, match="not in the section set"):
        _render([StepResult("s1", "ran", stdout="x")], [_spec("s1", heading="### Surprise")])


def test_manifest_step_absent_from_ledger_raises():
    with pytest.raises(ValueError, match="no ledger row"):
        _render([StepResult("s1", "ran")], [_spec("s1"), _spec("s2")])


def test_silent_step_and_cadence_skipped_render_nothing_and_not_run_lists_rest():
    steps = [_spec("s1"), _spec("s2"), _spec("s3")]
    rows = [
        StepResult("s1", "ran", stdout="  \n", process_ms=10, spawns=1),
        StepResult("s2", "cadence-skipped"),
        {"id": "s3", "status": "not-composed", "reason": "BUG-9"},
    ]
    md = _render(rows, steps)
    assert "### Orphan Sweep" not in md
    assert "`s3` (not-composed): BUG-9" in md and "s2" not in md
    assert "1 steps run; 1 spawns; slowest `s1` 10ms" in md


def test_engine_step_over_bar_named_in_run_cost():
    md = _render([StepResult("s1", "ran", process_ms=900.0, spawns=2)], [_spec("s1")])
    assert "engine over 500ms: `s1` 900ms" in md
