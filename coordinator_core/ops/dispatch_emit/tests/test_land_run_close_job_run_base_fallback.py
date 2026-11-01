"""The close-job record's run_base_sha falls back to the run's recorded base when the review prep has none."""
from pathlib import Path

from coordinator_core.ops.dispatch_emit.ask_fanout_contract import run_base_rel
from coordinator_core.ops.dispatch_emit.land_run import _close_job_run_base

PREP = "a" * 40
RECORDED = "b" * 40
RUN = "ask-20261101T000000-abc123"


def _record(root: Path, text: str) -> None:
    path = root / run_base_rel(RUN)
    path.parent.mkdir(parents=True)
    path.write_text(text + "\n", encoding="utf-8")


def test_prep_base_wins_over_recorded(tmp_path):
    _record(tmp_path, RECORDED)
    assert _close_job_run_base(tmp_path, PREP, RUN) == PREP


def test_missing_prep_base_falls_back_to_recorded(tmp_path):
    _record(tmp_path, RECORDED)
    assert _close_job_run_base(tmp_path, None, RUN) == RECORDED


def test_short_prep_base_falls_back_to_recorded(tmp_path):
    _record(tmp_path, RECORDED)
    assert _close_job_run_base(tmp_path, "abc1234", RUN) == RECORDED


def test_nothing_recorded_is_none(tmp_path):
    assert _close_job_run_base(tmp_path, None, RUN) is None
    assert _close_job_run_base(tmp_path, None, None) is None


def test_recorded_non_sha_is_refused(tmp_path):
    _record(tmp_path, "not-a-sha")
    assert _close_job_run_base(tmp_path, None, RUN) is None
