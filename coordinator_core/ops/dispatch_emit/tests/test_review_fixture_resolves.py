"""The shared review fixture resolves against the real floor, and a fixture without a sidecar still yields the floor note."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import review_floor, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest
from coordinator_core.ops.dispatch_emit.tests._review_fixture import REVIEW_SIDECAR, reviewed, unreviewed

_TESTS = Path(__file__).resolve().parent
_FLOOR_FILES = (
    "test_review_floor.py",
    "test_terminal_commit_review_floor.py",
    "test_land_run_review_floor.py",
    "test_requirements_ledger_review_floor.py",
    "test_ask_compose_xs_code_review.py",
    "test_ask_stage_review_code_paths.py",
    "test_phase_ledger.py",
)


def _code_request() -> CommitRequest:
    return CommitRequest(
        chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), deliverable_id="d", plan_path="p.md"
    )


def test_sidecar_is_an_absolute_existing_file():
    assert Path(REVIEW_SIDECAR).is_absolute()
    assert Path(REVIEW_SIDECAR).is_file()


def test_reviewed_counts_one_verdict_without_the_sidecar_under_root(tmp_path):
    assert review_floor.verdict_count(reviewed(), tmp_path) == 1


def test_unreviewed_is_still_refused(tmp_path):
    assert terminal_commit._review_floor_note(_code_request(), unreviewed(), False, tmp_path) is not None
    assert review_floor.floor_reason(["a.py"], review_floor.verdict_count(unreviewed(), tmp_path)) is not None


def test_reviewed_with_missing_sidecar_is_still_refused(tmp_path):
    review = reviewed()
    review["wave_reviews"][0]["sidecar_path"] = str(tmp_path / "missing.md")
    assert terminal_commit._review_floor_note(_code_request(), review, False, tmp_path) is not None


def test_reviewed_passes_the_floor(tmp_path):
    assert terminal_commit._review_floor_note(_code_request(), reviewed(), False, tmp_path) is None


@pytest.mark.parametrize("name", _FLOOR_FILES)
def test_floor_test_files_do_not_import_reviewed(name):
    path = _TESTS / name
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    imports = [ln for ln in text.splitlines() if "_review_fixture" in ln]
    assert not any(re.search(r"\breviewed\b", ln) for ln in imports)


def test_reviewed_merges_extra_keys_over_the_base(tmp_path):
    review = reviewed(fixes=2, extra_key="x")
    assert review["fixes"] == 2 and review["extra_key"] == "x"
    assert review["integration_stem"] == "rev-stem"
    assert review_floor.verdict_count(review, tmp_path) == 1
    assert "extra_key" not in reviewed()


def test_reviewed_returns_a_fresh_dict_each_call():
    assert reviewed() is not reviewed()
    a = reviewed()
    a["wave_reviews"].clear()
    assert len(reviewed()["wave_reviews"]) == 1
