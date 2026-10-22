"""Pins remaining_leg_plan: dispatch, refuse, skip and token normalisation."""

from __future__ import annotations

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


def test_run_3672d0_shape_dispatches_with_sizing(tmp_path):
    root = _repo(tmp_path)
    rem = _rem(
        f"Update the sizing's falsifier and the ledger row to the rule, in {SIZING}",
        "Work that changed since 4257b125f8: none for R1, so the halt-judged diff cannot count as met",
    )
    plan = remaining_leg_plan(rem, [], [], [], SIZING, root)
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == [SIZING]
    assert plan["unwritable"] == []


def test_no_path_no_notes_refuses(tmp_path):
    root = _repo(tmp_path)
    plan = remaining_leg_plan(_rem("do the thing"), [], [], [], SIZING, root)
    assert plan["action"] == LEG_REFUSE
    assert "R1" in plan["reason"]


def test_no_path_with_note_skips(tmp_path):
    root = _repo(tmp_path)
    plan = remaining_leg_plan(_rem("do the thing"), ["re-ruled"], [], [], SIZING, root)
    assert plan["action"] == LEG_SKIP
    assert plan["writes"] == []


def test_product_path_outside_writes_refuses(tmp_path):
    root = _repo(tmp_path)
    plan = remaining_leg_plan(_rem("fix pkg/mod.py"), [], [], [], SIZING, root)
    assert plan["action"] == LEG_REFUSE
    assert plan["unwritable"] == ["pkg/mod.py"]


def test_product_path_in_manifest_writes_dispatches(tmp_path):
    root = _repo(tmp_path)
    plan = remaining_leg_plan(_rem("fix pkg/mod.py"), [], ["pkg/mod.py"], [], SIZING, root)
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == ["pkg/mod.py"]


def test_prose_abbreviations_are_not_paths(tmp_path):
    root = _repo(tmp_path)
    plan = remaining_leg_plan(_rem("tighten the rule, e.g. the falsifier (i.e. v1.5) in the sizing"), ["re-ruled"], [], [], SIZING, root)
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
    plan = remaining_leg_plan(rem, [], [], [], SIZING, root)
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == [SIZING]
    assert plan["unwritable"] == []
