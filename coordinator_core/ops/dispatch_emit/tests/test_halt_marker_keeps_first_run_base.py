"""A re-halt never moves the run's base: the first one wins (the later HEADs are its own halt-record commits)."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import recorded_run_base, run_base_rel
from coordinator_core.ops.dispatch_emit.halted_run import HALT_RUN_BASE_FIELD, HALTED_FILE, write_halt_marker

RUN = "warp-2026-11-20-x-d4ad83-20261120T235030-9f094f"
FIRST, SECOND, THIRD, FILE_BASE = "1" * 40, "2" * 40, "3" * 40, "f" * 40


def _halt(root: Path, base: str) -> dict:
    (root / "scratch" / "warp" / RUN).mkdir(parents=True, exist_ok=True)
    rd = record_dir(root, RUN)
    write_halt_marker(rd, halted="dependents_unstarted", sizing_rel=None, run_base_sha=base)
    return json.loads((rd / HALTED_FILE).read_text(encoding="utf-8"))


def test_two_successive_halts_keep_the_first_base(tmp_path):
    assert _halt(tmp_path, FIRST)[HALT_RUN_BASE_FIELD] == FIRST
    assert _halt(tmp_path, SECOND)[HALT_RUN_BASE_FIELD] == FIRST
    assert _halt(tmp_path, THIRD)[HALT_RUN_BASE_FIELD] == FIRST


def test_the_first_halt_backfills_the_missing_run_base_file(tmp_path):
    _halt(tmp_path, FIRST)
    assert recorded_run_base(tmp_path, RUN) == FIRST
    _halt(tmp_path, SECOND)
    assert recorded_run_base(tmp_path, RUN) == FIRST


def test_the_recorded_run_base_file_beats_a_drifted_marker(tmp_path):
    _halt(tmp_path, SECOND)
    path = tmp_path / run_base_rel(RUN)
    path.write_text(FILE_BASE + "\n", encoding="utf-8")
    assert _halt(tmp_path, THIRD)[HALT_RUN_BASE_FIELD] == FILE_BASE


def test_a_resume_with_no_run_base_file_keeps_the_halted_base_not_head(tmp_path):
    _halt(tmp_path, FIRST)
    (tmp_path / run_base_rel(RUN)).unlink()
    scratch = tmp_path / "scratch" / "warp" / RUN
    (scratch / "submission.json").write_text(json.dumps({"run_base_sha": SECOND}), encoding="utf-8")

    assert _halt(tmp_path, SECOND)[HALT_RUN_BASE_FIELD] == FIRST
    assert recorded_run_base(tmp_path, RUN) == FIRST


def test_the_recorded_file_precedes_a_refire_submission_base(tmp_path):
    scratch = tmp_path / "scratch" / "warp" / RUN
    scratch.mkdir(parents=True)
    (scratch / "run-base").write_text(FIRST + "\n", encoding="utf-8")
    (scratch / "submission.json").write_text(json.dumps({"run_base_sha": SECOND}), encoding="utf-8")

    assert landing_retest.run_base_for(tmp_path, RUN, {}) == FIRST
