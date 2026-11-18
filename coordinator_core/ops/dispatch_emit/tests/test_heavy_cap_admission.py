"""A row declaring a leg kill cap over the self-admit tier is owed to the Group EM, never spawned."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_stage_plan
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import slot_withheld_rows
from coordinator_core.ops.dispatch_emit.spine_read import leg_exceeds_self_admit_tier, with_leg_slot

PLAN_REL = "docs/plans/2026-10-01-heavy-cap.md"


def _leg(cap: float) -> str:
    return f"  leg:\n    expected_peak_gib: {cap - 2:g}\n    kill_cap_gib: {cap:g}\n    wall_minutes: 30\n"


def _row(rid: str, extra: str = "") -> str:
    return (
        f"- id: {rid}\n  title: row {rid}\n  change_kind: code-edit\n  surface: pkg/{rid}.py\n"
        f"  writes:\n    - pkg/{rid}.py\n{extra}"
    )


def _plan(rows: list[str]) -> str:
    return "---\ndeliverable_id: dlv-cap-abc123\n---\n# Cap plan\n\n## Tasks\n\n```yaml plan-tasks\n" + "".join(rows) + "```\n"


def _stage(tmp_path: Path, rows: list[str]):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_plan(rows), encoding="utf-8", newline="\n")
    return stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)


def test_tier_boundary() -> None:
    assert leg_exceeds_self_admit_tier({"leg": {"kill_cap_gib": 24}})
    assert not leg_exceeds_self_admit_tier({"leg": {"kill_cap_gib": 20}})
    assert not leg_exceeds_self_admit_tier({"leg": {"kill_cap_gib": 16}})
    assert leg_exceeds_self_admit_tier({"leg": {"kill_cap_gib": "big"}})
    assert not leg_exceeds_self_admit_tier({"id": "C1"})


def test_with_leg_slot_marks_only_the_over_tier_row() -> None:
    assert with_leg_slot({"leg": {"kill_cap_gib": 24}})["needs_slot"] is True
    assert "needs_slot" not in with_leg_slot({"leg": {"kill_cap_gib": 16}})


def test_stage_plan_parse_reads_an_over_tier_leg_as_needs_slot(tmp_path: Path) -> None:
    text = _plan([_row("C1", _leg(24)), _row("C2", _leg(16))])
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    plan = tmp_path / PLAN_REL
    plan.write_text(text, encoding="utf-8", newline="\n")
    parse = ask_stage_plan._parse_plan(tmp_path, plan, text)
    assert parse.raw_by_id["C1"].get("needs_slot") is True
    assert not parse.raw_by_id["C2"].get("needs_slot")
    assert [r["id"] for r in slot_withheld_rows(parse.raw_by_id)] == ["C1"]


def _emit(tmp_path: Path, rows: list[str]) -> str:
    from coordinator_core.ops.dispatch_emit.emit import emit_script

    from .conftest import REVIEW_KW

    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / "p.md"
    plan.write_text(_plan(rows), encoding="utf-8", newline="\n")
    return emit_script(plan, repo_root=tmp_path, **REVIEW_KW)


def test_emit_owes_the_24_gib_row_with_declared_numbers_and_not_the_16_gib_row(tmp_path: Path) -> None:
    script = _emit(tmp_path, [_row("C1", _leg(24)), _row("C2", _leg(16))])
    narration = next(line for line in script.splitlines() if "ROWS HOLDING A SCARCE SLOT" in line)
    assert "C1 (declared peak 22 GiB, kill cap 24 GiB, wall 30 min)" in narration
    assert "C2" not in narration
    assert script.count("Slot row (needs_slot): author only") == 1
    assert "Declared leg: declared peak 22 GiB, kill cap 24 GiB, wall 30 min" in script


def test_brief_head_states_the_self_admit_tier() -> None:
    from coordinator_core.ops.dispatch_emit import emit

    for head in (emit._ROW_PROMPT_HEAD, emit._VERIFICATION_ROW_PROMPT_HEAD):
        assert "kill cap is at most 20 GiB (spawn gate kill+5)" in head
        assert "never launch it" in head


def test_stage_owes_a_24_gib_row_and_lets_a_16_gib_row_proceed(tmp_path: Path) -> None:
    m = _stage(tmp_path, [_row("C1", _leg(24)), _row("C2", _leg(16)), _row("C3"), _row("C4")])
    assert [s["id"] for s in m.slot_withheld] == ["C1"]
    by_id = {r.id: r for r in m.rows}
    assert by_id["C1"].group == ""
    assert "C2" not in {s["id"] for s in m.slot_withheld}
    assert by_id["C2"].group == by_id["C3"].group != ""
