"""A refire whose reserved run is a halted run of the sizing resumes it, though the sizing lookup prefers a sibling halt."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import HALTED_ID, SIZING_REL, _repo

pytestmark = pytest.mark.complete

NEWER_ID = "warp-2026-10-29-resume-fixture-20261029T050505-def456"
OTHER_SIZING = "state/sizings/2026-10-29-unrelated.yaml"
OTHER_ID = "warp-2026-10-29-unrelated-20261029T060606-aaa111"
SLICE_PARENT_RUN = "ask-20261029T070707-bbb222"
SIZING_DOC = {"em_analysis": {"ask_parent": f"ask-20261029T080808-ccc333 {SLICE_PARENT_RUN}"}}


def _two_halts(tmp_path: Path) -> Path:
    root = _repo(tmp_path, record=False)
    hr.mark_halted(root / RECORD_DIR_ROOT / NEWER_ID, SIZING_REL)
    return root


def _hint(root: Path, reserved: str) -> bool:
    return op._is_halt_resume_hint(root, reserved, SIZING_DOC, SIZING_REL)


def test_the_lookup_prefers_the_newer_halt_so_the_older_reserved_run_mismatches(tmp_path):
    root = _two_halts(tmp_path)
    assert hr.halted_run_to_resume(root, SIZING_REL) == NEWER_ID != HALTED_ID


def test_a_reserved_run_that_is_an_older_halt_of_the_sizing_is_a_resume_hint(tmp_path):
    root = _two_halts(tmp_path)
    assert _hint(root, HALTED_ID) is True
    assert _hint(root, NEWER_ID) is True


def test_a_revised_sibling_halt_does_not_hide_the_reserved_run(tmp_path):
    root = _two_halts(tmp_path)
    assert hr.mark_revised(root / RECORD_DIR_ROOT / NEWER_ID, "2026-10-29T09:00:00Z")
    assert _hint(root, HALTED_ID) is True


def test_a_revised_older_halt_is_a_resume_hint_beside_a_newer_halt(tmp_path):
    root = _two_halts(tmp_path)
    assert hr.mark_revised(root / RECORD_DIR_ROOT / HALTED_ID, "2026-10-29T09:00:00Z")
    assert _hint(root, HALTED_ID) is True
    assert _hint(root, NEWER_ID) is True


def test_a_halted_run_of_another_sizing_is_still_refused(tmp_path):
    root = _two_halts(tmp_path)
    hr.mark_halted(root / RECORD_DIR_ROOT / OTHER_ID, OTHER_SIZING)
    assert _hint(root, OTHER_ID) is False
    assert hr._read(root / RECORD_DIR_ROOT / OTHER_ID)["sizing_rel"] == OTHER_SIZING


def test_an_unknown_run_id_is_still_refused(tmp_path):
    root = _two_halts(tmp_path)
    assert _hint(root, "warp-2026-10-29-resume-fixture-20261029T090909-000000") is False


def test_a_run_that_is_no_longer_halted_is_still_refused(tmp_path):
    root = _two_halts(tmp_path)
    assert hr.mark_closed(root / RECORD_DIR_ROOT / HALTED_ID, "closed by test", "2026-10-29T09:00:00Z")
    assert _hint(root, HALTED_ID) is False


def test_the_slices_own_reservation_is_never_a_hint(tmp_path):
    root = _two_halts(tmp_path)
    hr.mark_halted(root / RECORD_DIR_ROOT / SLICE_PARENT_RUN, SIZING_REL)
    assert _hint(root, SLICE_PARENT_RUN) is False


def test_the_older_reserved_run_passes_the_hint_that_emit_takes_before_reserved_run(tmp_path):
    root = _two_halts(tmp_path)
    with pytest.raises(ValueError, match="is not the run"):
        op._reserved_run(root, HALTED_ID, SIZING_DOC, SIZING_REL)
    assert _hint(root, HALTED_ID) is True
