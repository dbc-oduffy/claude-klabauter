"""land_run_leg_budget_profile: parse counting, the kill gate, and the row shape over a small fixture."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.benchmarks import land_run_leg_budget_profile as lb


def test_parse_counter_counts_sizing_and_spine_not_other_text() -> None:
    counter = lb.SpineParseCounter()
    counter.see(f"{lb.SIZING_MARK}\nplan: x.md\n")
    counter.see("- id: S0\n  writes: []\n")
    counter.see("unrelated: 1\n")
    counter.see(b"- id: S1\n")
    assert (counter.sizing, counter.plan, counter.total) == (1, 2, 3)


def test_kill_gate_pins_both_sides() -> None:
    over = [lb.AxisRow(lb.WHOLE_OP, lb.BAR_MS + 1)]
    under = [lb.AxisRow(lb.WHOLE_OP, lb.BAR_MS), lb.AxisRow(lb.AXIS_HOLDS, lb.BAR_MS * 4)]
    assert lb.kill_gate(over) is True
    assert lb.kill_gate(under) is False


def test_rows_over_small_fixture(tmp_path: Path, monkeypatch) -> None:
    for name in ("COORDINATOR_INVOCATION_ORIGIN", "COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.setenv(name, "x")
    siblings = 2
    rows = lb.run(tmp_path, siblings, tracked=20, chain=3, attribute=False)
    by_axis = {r.axis: r for r in rows}
    assert set(by_axis) == {lb.WHOLE_OP, lb.AXIS_READ_TO_INTEGRITY, lb.AXIS_HOLDS, lb.AXIS_SPINE}
    for row in rows:
        assert row.process_time_ms >= 0.0 and row.spawns >= 0
        assert row.attribution["siblings"] == siblings
    holds = by_axis[lb.AXIS_HOLDS]
    assert holds.attribution["measured"] is True
    assert holds.spine_parses >= siblings
    whole = by_axis[lb.WHOLE_OP]
    assert whole.spine_parses >= holds.spine_parses
    assert whole.attribution["sizing_parses"] >= siblings and whole.attribution["plan_parses"] >= siblings
