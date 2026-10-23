"""closing_bases, terminal_plan_closures and the Run-Closed/Run-Halted trailer-line helpers."""

import json

from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
    run_closed_bases,
    run_closed_trailer_lines,
    run_halted_bases,
    run_halted_trailer_lines,
    terminal_plan_closures,
)
from coordinator_core.ops.dispatch_emit.landing_integrity import closing_bases

H1 = "a" * 40
H2 = "b" * 40
H3 = "c" * 40
PLAN = "docs/plans/2026-10-23-x.md"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_manifest_first_then_submission_then_review(tmp_path):
    _write(tmp_path / "state/warp/run-1/manifest.json", {"head_sha": H1})
    _write(tmp_path / "scratch/warp/run-1/submission.json", {"run_base_sha": H2})
    assert closing_bases(tmp_path, "run-1", {"review_run_base": H3}) == (H1, H2, H3)


def test_dedupes_equal_bases(tmp_path):
    _write(tmp_path / "state/warp/run-1/manifest.json", {"head_sha": H1})
    _write(tmp_path / "scratch/warp/run-1/submission.json", {"run_base_sha": H1})
    assert closing_bases(tmp_path, "run-1", {"review_run_base": H1}) == (H1,)


def test_workflow_suffix_resolves_same_dir(tmp_path):
    _write(tmp_path / "state/warp/run-1/manifest.json", {"head_sha": H1})
    assert closing_bases(tmp_path, "run-1.workflow", {}) == (H1,)


def test_empty_when_nothing_resolves(tmp_path):
    assert closing_bases(tmp_path, "run-1", {}) == ()
    assert closing_bases(tmp_path, None, {}) == ()


def test_non_hex_dropped(tmp_path):
    _write(tmp_path / "state/warp/run-1/manifest.json", {"head_sha": "not-a-sha"})
    assert closing_bases(tmp_path, "run-1", {"review_run_base": "abc1234"}) == ()


def test_landing_closes_touched_plan():
    msg = "land: x\n\nLands-Chunks: C0"
    assert terminal_plan_closures(msg, [PLAN, "coordinator_core/a.py"]) == {PLAN}


def test_halt_subject_single_and_reaper_form():
    single = "warp: record halted run-1 run artifacts\n\nHalted-By: x"
    assert terminal_plan_closures(single, [PLAN]) == {PLAN}
    reaper = "warp: record halted run-1, run-2 run artifacts\n\nHalted-By: x"
    assert terminal_plan_closures(reaper, [PLAN]) == {PLAN}


def test_halt_plan_trailer_returned_without_path():
    msg = f"warp: record halted run-1 run artifacts\n\nHalted-By: x\nHalt-Plan: {PLAN}"
    assert terminal_plan_closures(msg, []) == {PLAN}


def test_checkpoint_stamp_and_untyped_return_empty():
    checkpoint = (
        "checkpoint(wave 0): 1 row — C0\n\n"
        f"Checkpoint-Plan: {PLAN}\nLands-Chunks: C0"
    )
    assert terminal_plan_closures(checkpoint, [PLAN]) == frozenset()
    assert terminal_plan_closures(f"mark 1 rows coded (abc1234)\n\nCheckpoint-Stamp: {H1}", [PLAN]) == frozenset()
    assert terminal_plan_closures("edit the plan", [PLAN]) == frozenset()


def test_trailer_lines_drop_non_hex_and_dedupe():
    assert run_closed_trailer_lines([H1, "short", H1, H2]) == [
        f"Checkpoint-Run-Closed: {H1}", f"Checkpoint-Run-Closed: {H2}"]
    assert run_halted_trailer_lines([H2, "zz", H2]) == [f"Checkpoint-Run-Halted: {H2}"]


def test_halted_bases_round_trip_and_stay_apart_from_closed():
    msg = "\n".join(run_halted_trailer_lines([H1, H2]))
    assert run_halted_bases(msg) == {H1, H2}
    assert run_closed_bases(msg) == frozenset()
