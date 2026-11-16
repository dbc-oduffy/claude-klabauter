"""A refire binds the newest halted run of a sizing; a revise stamp wins only when no halt came after it.

Regression: example-game-repo-fc refire resolved to the revised 11-13 halt over the 11-16 halt that had just occurred.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_run as hr

SIZING = "state/sizings/2026-11/2026-11-13-example-stats-repo.yaml"
STEM = "2026-11-13-example-stats-repo"
OLD = f"warp-{STEM}-20261113T205800-4c17bc"
NEW = f"warp-{STEM}-20261116T104255-ee21f7"
ASK = "ask-20261116T104255-ab12cd"


def _halt(root: Path, run_id: str, revised_at: str | None = None) -> None:
    d = root / hr.RECORD_DIR_ROOT / run_id
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    if revised_at:
        assert hr.mark_revised(d, revised_at)


def test_prefer_newer_halt_beats_older_revise(tmp_path: Path) -> None:
    _halt(tmp_path, OLD, revised_at="2026-11-14T09:00:00Z")
    _halt(tmp_path, ASK)
    assert hr._prefer(tmp_path, SIZING, [OLD, ASK], None) == ASK


def test_prefer_revise_newer_than_every_halt_still_wins(tmp_path: Path) -> None:
    _halt(tmp_path, OLD, revised_at="2026-11-17T09:00:00Z")
    _halt(tmp_path, ASK)
    assert hr._prefer(tmp_path, SIZING, [OLD, ASK], None) == OLD


def test_resume_two_warp_halts_binds_newest_over_older_revise(tmp_path: Path) -> None:
    _halt(tmp_path, OLD, revised_at="2026-11-14T09:00:00Z")
    _halt(tmp_path, NEW)
    assert hr.halted_run_to_resume(tmp_path, SIZING) == NEW


def test_resume_two_warp_halts_revise_after_newest_halt_wins(tmp_path: Path) -> None:
    _halt(tmp_path, OLD, revised_at="2026-11-17T09:00:00Z")
    _halt(tmp_path, NEW)
    assert hr.halted_run_to_resume(tmp_path, SIZING) == OLD
