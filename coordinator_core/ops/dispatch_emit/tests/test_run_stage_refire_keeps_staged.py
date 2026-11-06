"""A refire or resume of a staged run keeps run-stage `staged`; every stage stamp advances `last_activity_at`."""

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract as c
from coordinator_core.ops.dispatch_emit import requirements_ledger

RUN = "run-refire"


def _stage(root):
    return c.read_run_stage(root, RUN)


def test_new_run_still_mints(tmp_path):
    requirements_ledger.start(tmp_path, RUN)
    assert _stage(tmp_path)["stage"] == "minted"


def test_refire_of_staged_run_keeps_staged_and_started_at(tmp_path):
    requirements_ledger.start(tmp_path, RUN)
    rec = c.record_dir(tmp_path, RUN)
    c.write_run_stage_at(rec, "staged", steps_planned=3, now="2026-11-06T10:00:00Z")
    first = _stage(tmp_path)
    requirements_ledger.start(tmp_path, RUN)
    after = _stage(tmp_path)
    assert after["stage"] == "staged"
    assert after["started_at"] == first["started_at"]
    assert after["staged_at"] == "2026-11-06T10:00:00Z"
    assert after["steps_planned"] == 3
    assert after["last_activity_at"] > "2026-11-06T10:00:00Z"


def test_refire_of_halted_run_with_staged_at_reads_staged(tmp_path):
    rec = c.record_dir(tmp_path, RUN)
    c.write_run_stage_at(rec, "minted", now="2026-11-06T09:00:00Z")
    c.write_run_stage_at(rec, "staged", now="2026-11-06T10:00:00Z")
    c.write_run_stage_at(rec, "halted", now="2026-11-06T11:00:00Z")
    requirements_ledger.start(tmp_path, RUN)
    assert _stage(tmp_path)["stage"] == "staged"


def test_halted_run_never_staged_is_still_minted_on_refire(tmp_path):
    rec = c.record_dir(tmp_path, RUN)
    c.write_run_stage_at(rec, "halted", now="2026-11-06T11:00:00Z")
    requirements_ledger.start(tmp_path, RUN)
    assert _stage(tmp_path)["stage"] == "minted"


def test_landed_run_is_not_reopened(tmp_path):
    rec = c.record_dir(tmp_path, RUN)
    c.write_run_stage_at(rec, "landed", now="2026-11-06T11:00:00Z")
    requirements_ledger.start(tmp_path, RUN)
    assert _stage(tmp_path)["stage"] == "landed"


def test_staged_survives_later_mint_stamps(tmp_path):
    rec = c.record_dir(tmp_path, RUN)
    c.write_run_stage_at(rec, "staged", now="2026-11-06T10:00:00Z")
    c.stamp_run_stage_at(rec, "minted")
    c.stamp_run_stage_at(rec, "minted")
    assert _stage(tmp_path)["stage"] == "staged"


def test_stage_completion_advances_last_activity(tmp_path):
    rec = c.record_dir(tmp_path, RUN)
    c.write_run_stage_at(rec, "minted", now="2026-11-06T10:00:00Z")
    c.write_run_stage_at(rec, "staged", now="2026-11-06T10:05:00Z")
    data = _stage(tmp_path)
    assert data["started_at"] == "2026-11-06T10:00:00Z"
    assert data["last_activity_at"] == "2026-11-06T10:05:00Z"


def test_adopt_orphaned_run_path_keeps_staged(tmp_path):
    rec = c.record_dir(tmp_path, RUN)
    c.write_run_stage_at(rec, "staged", session_id="old-session", now="2026-11-06T10:00:00Z")
    c.adopt_run_stage_at(rec, "new-session", now="2026-11-06T10:30:00Z")
    data = _stage(tmp_path)
    assert data["stage"] == "staged"
    assert data["session_id"] == "new-session"
    assert data["adopted_from"] == ["old-session"]
    assert data["last_activity_at"] == "2026-11-06T10:30:00Z"


def test_unknown_stage_still_refused(tmp_path):
    with pytest.raises(ValueError):
        c.write_run_stage_at(c.record_dir(tmp_path, RUN), "executing")
    assert c.RUN_STAGES == ("minted", "staged", "halted", "landed")
