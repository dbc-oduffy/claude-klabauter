"""ask_stage groups small same-wave rows into shared-agent groups and reports the manifest fanout."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import stage

PLAN_REL = "docs/plans/2026-10-01-fanout.md"


def _plan(rows: list[str]) -> str:
    return "---\ndeliverable_id: dlv-fan-abc123\n---\n# Fan plan\n\n## Tasks\n\n```yaml plan-tasks\n" + "".join(rows) + "```\n"


def _row(rid: str, extra: str = "", body: str = "") -> str:
    text = (
        f"- id: {rid}\n  title: row {rid}\n  change_kind: code-edit\n  surface: pkg/{rid}.py\n"
        f"  writes:\n    - pkg/{rid}.py\n{extra}"
    )
    if body:
        text += "  body: |\n" + "".join(f"    {ln}\n" for ln in body.splitlines())
    return text


def _stage(tmp_path, rows):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_plan(rows), encoding="utf-8", newline="\n")
    return stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)


def test_six_small_same_wave_rows_fall_into_three_groups(tmp_path):
    m = _stage(tmp_path, [_row(f"C{i}") for i in range(1, 7)])
    groups = {r.group for r in m.rows}
    assert len(groups) == 3 and "" not in groups
    assert all(sum(1 for r in m.rows if r.group == g) == 2 for g in groups)
    assert m.fanout["expectedAgents"] == 3
    assert m.fanout["units"] == 6
    assert m.fanout["basis"] == "exact"
    assert sorted(len(g) for g in m.fanout["groups"]) == [2, 2, 2]


def test_group_id_is_wave_and_first_member(tmp_path):
    m = _stage(tmp_path, [_row(f"C{i}") for i in range(1, 7)])
    first = next(r for r in m.rows if r.id == "C1")
    assert first.group == "g0-C1"
    assert next(r for r in m.rows if r.id == "C2").group == "g0-C1"


def test_needs_slot_and_large_rows_stay_solo(tmp_path):
    rows = [
        _row("C1", "  needs_slot: true\n"),
        _row("C2").replace("title: row C2", "title: " + "w " * 6000),
        _row("C3"),
        _row("C4"),
        _row("C5"),
    ]
    m = _stage(tmp_path, rows)
    by_id = {r.id: r for r in m.rows}
    assert by_id["C1"].group == "" and by_id["C2"].group == ""
    assert by_id["C3"].group == by_id["C4"].group != ""
    assert by_id["C5"].group == ""
    assert m.fanout["expectedAgents"] == 4


def test_rows_with_different_deps_or_waves_never_share_a_group(tmp_path):
    rows = [
        _row("C1"),
        _row("C2"),
        _row("C3"),
        _row("C4", "  depends_on:\n    - chunk: C1\n      gate_kind: output-consumption-runtime\n"),
        _row("C5", "  depends_on:\n    - chunk: C2\n      gate_kind: output-consumption-runtime\n"),
        _row("C6", "  depends_on:\n    - chunk: C2\n      gate_kind: output-consumption-runtime\n"),
    ]
    m = _stage(tmp_path, rows)
    by_id = {r.id: r for r in m.rows}
    assert by_id["C4"].group == ""
    assert by_id["C5"].group == by_id["C6"].group != ""
    for g in {r.group for r in m.rows if r.group}:
        members = [r for r in m.rows if r.group == g]
        assert len({(r.wave, r.deps, r.agent_type, r.model) for r in members}) == 1


def test_fewer_than_three_rows_get_no_group(tmp_path):
    m = _stage(tmp_path, [_row("C1"), _row("C2")])
    assert all(r.group == "" for r in m.rows)
    assert m.fanout["expectedAgents"] == 2
