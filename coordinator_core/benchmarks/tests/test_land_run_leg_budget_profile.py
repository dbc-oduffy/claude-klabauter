"""land_run_leg_budget_profile: parse counting, the kill gate, and the row shape over a small fixture."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.benchmarks import land_run_leg_budget_profile as lb


def _bind_env(monkeypatch) -> None:
    for name in ("COORDINATOR_INVOCATION_ORIGIN", "COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.setenv(name, "x")


def test_census_lists_the_landing_path_legs() -> None:
    census = lb.census_legs()
    assert len(census) >= 13
    assert {"peer_hunk_gate.co_declared_peer_holds", "land_run.commit", "land_run.script_read",
            "landing_integrity.since_base_findings"} <= set(census)
    prof = lb.Profiler(attribute=False)
    by_leg = {r.attribution["leg"]: r for r in lb.leg_rows(prof, 0.0, 0)}
    assert by_leg["land_run._integrity.landing_test_gate"].attribution["excepted"] is True
    assert by_leg["land_run.commit"].attribution["excepted"] is False


def test_leg_costs_sum_repeat_calls_and_leg_rows_mark_unreached() -> None:
    prof = lb.Profiler(attribute=False)
    prof.marks = [("a", 10.0, 1), ("b", 30.0, 3), ("a", 40.0, 3)]
    costs = prof.leg_costs(end_ms=100.0, end_spawns=7)
    assert costs["a"] == {"process_time_ms": 80.0, "spawns": 6, "calls": 2}
    assert costs["b"] == {"process_time_ms": 10.0, "spawns": 0, "calls": 1}
    prof.marks = [("land_run.commit", 5.0, 0), ("extra.leg", 6.0, 0)]
    by_leg = {r.attribution["leg"]: r for r in lb.leg_rows(prof, 9.0, 2)}
    assert by_leg["land_run.commit"].attribution["measured"] is True
    assert by_leg["extra.leg"].attribution["in_census"] is False
    unreached = by_leg["land_run.script_read"]
    assert unreached.attribution["not_reached"] is True and unreached.process_time_ms == 0.0 and unreached.spawns == 0


def test_live_siblings_json_covers_every_census_leg(tmp_path: Path, monkeypatch, capsys) -> None:
    _bind_env(monkeypatch)
    lb.main(["--live-siblings", "2", "--tracked", "20", "--chain", "3", "--json", "--dir", str(tmp_path)])
    rows = json.loads(capsys.readouterr().out)
    legs = {r["axis"][len(lb.LEG_AXIS_PREFIX):]: r for r in rows if r["axis"].startswith(lb.LEG_AXIS_PREFIX)}
    assert set(lb.census_legs()) <= set(legs)
    for leg, row in legs.items():
        assert row["process_time_ms"] >= 0.0 and row["spawns"] >= 0
        assert row["attribution"]["measured"] != row["attribution"]["not_reached"]
    assert legs["land_run.script_read"]["attribution"]["measured"] is True
    whole = rows[0]["attribution"]
    assert rows[0]["axis"] == lb.WHOLE_OP and whole["live_siblings"] == 2
    assert set(whole["live_sibling_outcomes"]) == {"s0", "s1"}
    reached = {leg for leg, row in legs.items() if row["attribution"]["measured"]}
    assert reached == ({s["leg"] for s in whole["legs"]} | {s["to"] for s in whole["legs"]}) - {"start"}


def test_live_siblings_attribute_profiles_per_leg(tmp_path: Path, monkeypatch) -> None:
    _bind_env(monkeypatch)
    rows = lb.run_live(tmp_path, 1, 0, tracked=20, chain=3, attribute=True)
    script_read = next(r for r in rows if r.axis == lb.LEG_AXIS_PREFIX + lb.LEG_SCRIPT_READ)
    assert script_read.attribution["top_functions"]


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
