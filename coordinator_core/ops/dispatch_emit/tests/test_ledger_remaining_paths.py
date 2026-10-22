"""Pins remaining_leg_plan: dispatch, refuse, skip, token normalisation and row-footprint narrowing."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit.ask_contract import LEG_DISPATCH, LEG_REFUSE, LEG_SKIP
from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import remaining_leg_plan

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


def test_no_path_with_note_skips(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(_rem("do the thing"), ["re-ruled"], [], root)
    assert plan["action"] == LEG_SKIP
    assert plan["writes"] == []


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
    plan = _plan(_rem("tighten the rule, e.g. the falsifier (i.e. v1.5) in the sizing"), ["re-ruled"], [], root)
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
