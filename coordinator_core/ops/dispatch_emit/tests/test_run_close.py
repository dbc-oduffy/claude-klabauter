"""Tests for run_close: leg vocabulary, close-block builder, close-job record."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ops.dispatch_emit import run_close as rc
from coordinator_core.ops.dispatch_emit.run_close import LegResult

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def test_missing_leg_becomes_failed_and_outstanding():
    block = rc.build_close_block("r1", True, {"review": LegResult("ran")})
    assert block["legs"]["post-close"] == {"status": "failed", "detail": "leg did not run"}
    assert block["legs"]["review"]["status"] == "ran"
    assert set(block["legs"]) == set(rc.RUN_BOUND_LEGS) | set(rc.SESSION_BOUND_LEGS)
    assert block["legs"]["session-shape"]["status"] == "session-bound"


def test_outstanding_equals_failed_set():
    legs = {leg: LegResult("ran") for leg in rc.RUN_BOUND_LEGS}
    legs["lessons"] = LegResult("failed", "boom")
    legs["completion-entry"] = LegResult("failed", "no judge summary")
    block = rc.build_close_block("r1", True, legs, "entry.md")
    assert block["outstanding"] == ["lessons", "completion-entry"]
    assert block["completion_entry"] == "entry.md"
    assert block["run_id"] == "r1" and block["applies"] is True


def test_queued_is_not_outstanding():
    legs = {leg: LegResult("ran") for leg in rc.RUN_BOUND_LEGS}
    legs["baton-drain"] = LegResult("queued", "job record .coordinator-local/close-jobs/r1.json")
    assert rc.build_close_block("r1", True, legs)["outstanding"] == []


def test_not_applies_marks_every_run_bound_leg_not_applicable():
    block = rc.build_close_block("r1", False, {})
    assert block["outstanding"] == []
    assert {block["legs"][leg]["status"] for leg in rc.RUN_BOUND_LEGS} == {"not-applicable"}


@pytest.mark.parametrize(
    "detail",
    ["see C:\\w\\f.md", "see C:/w/f", "wrote /abs/f", "state\\a.md", "(/abs/path)"],  # abs-path-ok: refusal fixtures
)
def test_non_relative_path_in_detail_is_refused(detail):
    with pytest.raises(ValueError):
        LegResult("ran", detail)


def test_repo_relative_forward_slash_detail_is_accepted():
    LegResult("ran", "wrote state/lessons-outbox/a.md and/or b")


def test_unknown_status_refused():
    with pytest.raises(ValueError):
        LegResult("pending")


def test_close_job_path_is_forward_slash_relative():
    assert rc.close_job_path("r1") == ".coordinator-local/close-jobs/r1.json"


def test_read_close_job_missing_record_is_failed(tmp_path):
    got = rc.read_close_job(tmp_path, "r1", T0, legs=["baton-drain"])
    assert got == {"baton-drain": LegResult("failed", "job record missing")}


def test_read_close_job_late_job_is_failed(tmp_path):
    rc.write_close_job(tmp_path, "r1", ["baton-drain", "post-close"], T0 + timedelta(seconds=5))
    rc.record_leg_outcome(tmp_path, "r1", "post-close", "ran", "done")
    got = rc.read_close_job(tmp_path, "r1", T0 + timedelta(seconds=6))
    assert got["post-close"] == LegResult("ran", "done")
    assert got["baton-drain"].status == "failed"
    assert got["baton-drain"].detail.startswith("job not finished by ")


def test_read_close_job_before_deadline_stays_queued(tmp_path):
    rc.write_close_job(tmp_path, "r1", ["baton-drain"], T0 + timedelta(seconds=5))
    got = rc.read_close_job(tmp_path, "r1", T0)
    assert got["baton-drain"].status == "queued"
    assert ".coordinator-local/close-jobs/r1.json" in got["baton-drain"].detail


def test_record_is_json_with_relative_return(tmp_path):
    rel = rc.write_close_job(tmp_path, "r1", ["memo-lifecycle"], T0)
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert data["legs"] == {"memo-lifecycle": {"status": "queued", "detail": ""}}


def test_record_leg_outcome_rejects_unqueued_leg_and_missing_record(tmp_path):
    with pytest.raises(FileNotFoundError):
        rc.record_leg_outcome(tmp_path, "r1", "post-close", "ran")
    rc.write_close_job(tmp_path, "r1", ["post-close"], T0)
    with pytest.raises(ValueError):
        rc.record_leg_outcome(tmp_path, "r1", "baton-drain", "ran")


def test_corrupt_record_reads_as_missing(tmp_path):
    (tmp_path / rc.close_job_path("r1")).parent.mkdir(parents=True)
    (tmp_path / rc.close_job_path("r1")).write_text("{not json", encoding="utf-8")
    got = rc.read_close_job(tmp_path, "r1", T0, legs=["post-close"])
    assert got["post-close"].detail == "job record missing"
