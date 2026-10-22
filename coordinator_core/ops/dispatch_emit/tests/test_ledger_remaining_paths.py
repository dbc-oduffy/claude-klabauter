"""Pins remaining_leg_plan: dispatch, refuse, skip, token normalisation and row-footprint narrowing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import LEG_DISPATCH, LEG_REFUSE, LEG_SKIP
from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import (
    PlanRowsUnreadable,
    plan_rows_serving,
    remaining_leg_plan,
)

SIZING = "state/sizings/2026-10-22-x.yaml"


def _repo(tmp_path):
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / SIZING).write_text("x")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/mod.py").write_text("x")
    return tmp_path


def _rem(*texts):
    return {"R1": {"remaining": list(texts), "evidence": ""}}


def _row(writes, register_rows=("R1",)):
    return SimpleNamespace(writes=tuple(writes), register_rows=tuple(register_rows))


def _plan(rem, notes, writes, root):
    rows = [_row(writes)] if writes else []
    return remaining_leg_plan(rem, notes, rows, ["R1"], [], SIZING, root)


def test_run_3672d0_shape_dispatches_with_sizing(tmp_path):
    root = _repo(tmp_path)
    rem = _rem(
        f"Update the sizing's falsifier and the ledger row to the rule, in {SIZING}",
        "Work that changed since 4257b125f8: none for R1, so the halt-judged diff cannot count as met",
    )
    plan = _plan(rem, [], [], root)
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == [SIZING]
    assert plan["unwritable"] == []


def test_no_path_no_notes_refuses(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(_rem("do the thing"), [], [], root)
    assert plan["action"] == LEG_REFUSE
    assert "R1" in plan["reason"]


def test_no_path_with_sha_note_skips(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(_rem("do the thing"), ["met by 2fd9d96c"], [], root)
    assert plan["action"] == LEG_SKIP
    assert plan["writes"] == []


def test_sha_free_note_with_owed_work_dispatches(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(_rem("do the thing"), ["please finish it"], [], root)
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == []
    assert "re-plan leg" in plan["reason"]


def test_note_with_nothing_owed_skips(tmp_path):
    root = _repo(tmp_path)
    plan = remaining_leg_plan({"R1": {"remaining": []}}, ["finish"], [], ["R1"], [], SIZING, root)
    assert plan["action"] == LEG_SKIP


def test_plan_writes_merge_after_manifest_writes_deduplicated(tmp_path):
    root = _repo(tmp_path)
    rows = [_row(["pkg/mod.py"])]
    plan = remaining_leg_plan(
        _rem("do the thing"), ["finish"], rows, ["R1"], [], SIZING, root,
        plan_writes=["pkg/other.py", "pkg/mod.py"],
    )
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == ["pkg/mod.py", "pkg/other.py"]


_FM = '---\nregister_claims:\n  rows: ["R1"]\n---\n'


def _spine(rows_yaml):
    return f"{_FM}# P\n\n## Tasks\n\n```yaml plan-tasks\n{rows_yaml}```\n"


def test_plan_rows_serving_register_rows_and_claims_fallback(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text(_spine(
        "- id: C1\n  disposition: coded\n  writes: [a.py, b.py]\n"
        "- id: C2\n  disposition: open\n  register_rows: [R2]\n  writes: [c.py]\n"
        "- id: C3\n  disposition: open\n  register_rows: [R1]\n  writes: [b.py, d.py]\n"
    ))
    assert plan_rows_serving(p, ["R1"]) == ["a.py", "b.py", "d.py"]
    assert plan_rows_serving(p, ["R2"]) == ["c.py"]


def test_plan_rows_serving_excludes_wont_do_and_spun_off(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text(_spine(
        "- id: C1\n  disposition: wont_do\n  writes: [a.py]\n"
        "- id: C2\n  disposition: spun_off\n  writes: [b.py]\n"
        "- id: C3\n  disposition: coded\n  writes: [c.py]\n"
    ))
    assert plan_rows_serving(p, ["R1"]) == ["c.py"]


def test_plan_rows_serving_missing_or_unparseable_plan_raises(tmp_path):
    with pytest.raises(PlanRowsUnreadable, match="nope.md"):
        plan_rows_serving(tmp_path / "nope.md", ["R1"])
    bad = tmp_path / "bad.md"
    bad.write_text("no spine here")
    with pytest.raises(PlanRowsUnreadable, match="spine"):
        plan_rows_serving(bad, ["R1"])


def test_plan_rows_serving_does_not_swallow_other_exceptions(tmp_path, monkeypatch):
    p = tmp_path / "plan.md"
    p.write_text(_spine("- id: C1\n  writes: [a.py]\n"))
    import coordinator_core.ops.plan_tasks_render as ptr

    monkeypatch.setattr(ptr, "load_rows", lambda _t: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        plan_rows_serving(p, ["R1"])


def test_xs_arm_sha_free_note_with_owed_work_and_no_writes_refuses(tmp_path):
    root = _repo(tmp_path)
    plan = remaining_leg_plan(_rem("do the thing"), ["finish"], [], ["R1"], [], SIZING, root, arm="xs")
    assert plan["action"] == LEG_REFUSE
    assert "re-plan leg adds" not in plan["reason"]
    assert "arm xs" in plan["reason"]


def test_xs_arm_with_writes_still_dispatches(tmp_path):
    root = _repo(tmp_path)
    plan = remaining_leg_plan(
        _rem("do the thing"), ["finish"], [_row(["pkg/mod.py"])], ["R1"], [], SIZING, root, arm="xs"
    )
    assert plan["action"] == LEG_DISPATCH


def test_product_path_outside_writes_refuses(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(_rem("fix pkg/mod.py"), [], [], root)
    assert plan["action"] == LEG_REFUSE
    assert plan["unwritable"] == ["pkg/mod.py"]


def test_sha_bearing_note_lifts_unwritable_refusal(tmp_path):
    root = _repo(tmp_path)
    notes = ["R1 is met by 2fd9d96c (both parity tests green)"]
    plan = _plan(_rem("fix pkg/mod.py"), notes, [], root)
    assert plan["action"] == LEG_SKIP
    assert plan["unwritable"] == ["pkg/mod.py"]


def test_ten_char_sha_note_rerules_and_date_only_note_does_not(tmp_path):
    root = _repo(tmp_path)
    rem = _rem("fix pkg/mod.py")
    assert _plan(rem, ["met_by 2fd9d96c64"], [], root)["action"] != LEG_REFUSE
    assert _plan(rem, ["20261022"], [], root)["action"] == LEG_REFUSE


def test_note_without_sha_still_refuses_unwritable(tmp_path):
    root = _repo(tmp_path)
    notes = ["R1 is met, trust me; ran 20261022 and defaced nothing"]
    plan = _plan(_rem("fix pkg/mod.py"), notes, [], root)
    assert plan["action"] == LEG_REFUSE


def test_product_path_in_manifest_writes_dispatches(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(_rem("fix pkg/mod.py"), [], ["pkg/mod.py"], root)
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == ["pkg/mod.py"]


def test_only_rows_serving_blocking_id_contribute_writes(tmp_path):
    root = _repo(tmp_path)
    rows = [
        _row(["pkg/mod.py"], ["R1"]),
        _row(["pkg/other.py"], ["R2"]),
        _row(["pkg/none.py"], []),
    ]
    plan = remaining_leg_plan(_rem("fix pkg/mod.py"), [], rows, ["R1"], [], SIZING, root)
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == ["pkg/mod.py"]


def test_row_not_serving_blocking_leaves_named_path_unwritable(tmp_path):
    root = _repo(tmp_path)
    rows = [_row(["pkg/mod.py"], ["R2"])]
    plan = remaining_leg_plan(_rem("fix pkg/mod.py"), [], rows, ["R1"], [], SIZING, root)
    assert plan["action"] == LEG_REFUSE
    assert plan["unwritable"] == ["pkg/mod.py"]


def test_prose_abbreviations_are_not_paths(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(_rem("tighten the rule, e.g. the falsifier (i.e. v1.5) in the sizing"), ["met by 2fd9d96c"], [], root)
    assert plan["unwritable"] == []
    assert plan["action"] == LEG_SKIP


def test_token_normalisation(tmp_path):
    root = _repo(tmp_path)
    rem = _rem(
        f"`{SIZING}:12`,",
        "see /etc/passwd and ../x/pkg/mod.py and scratch/warp/a/b.md",
        "(state\\sizings\\2026-10-22-x.yaml)",
        f"{SIZING}#anchor.",
    )
    plan = _plan(rem, [], [], root)
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == [SIZING]
    assert plan["unwritable"] == []


def test_claude_md_only_skips_and_is_em_owed(tmp_path):
    root = _repo(tmp_path)
    (root / "CLAUDE.md").write_text("x")
    plan = _plan(_rem("add the rule to CLAUDE.md"), [], [], root)
    assert plan["action"] == LEG_SKIP
    assert plan["em_owed"] == ["CLAUDE.md"]
    assert plan["unwritable"] == []
    assert "CLAUDE.md" in plan["reason"]


def test_claude_md_plus_landable_write_dispatches(tmp_path):
    root = _repo(tmp_path)
    (root / "CLAUDE.md").write_text("x")
    plan = _plan(_rem(f"edit CLAUDE.md and {SIZING}"), [], [], root)
    assert plan["action"] == LEG_DISPATCH
    assert plan["em_owed"] == ["CLAUDE.md"]
    assert "CLAUDE.md" not in plan["writes"]
    assert plan["writes"] == [SIZING]


def test_claude_md_does_not_lift_nongated_unwritable_refusal(tmp_path):
    root = _repo(tmp_path)
    (root / "CLAUDE.md").write_text("x")
    plan = _plan(_rem("edit CLAUDE.md and fix pkg/mod.py"), [], [], root)
    assert plan["action"] == LEG_REFUSE
    assert plan["unwritable"] == ["pkg/mod.py"]
    assert plan["em_owed"] == ["CLAUDE.md"]
    assert plan["writes"] == []
