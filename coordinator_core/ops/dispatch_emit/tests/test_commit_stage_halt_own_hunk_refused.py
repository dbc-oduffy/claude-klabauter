"""A commit leg refused `own-hunk-stage-refused` halts the run even when its task ended `completed`."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.commit_stage_halt import HALT_COMMIT_STAGE
from coordinator_core.ops.dispatch_emit.tests.test_commit_stage_halt import (
    _journal,
    _land,
    _record_dir,
    _started,
)


def _result(result):
    return {"type": "result", "agentId": "a1", "key": "k1", "result": result}


def test_own_hunk_refusal_on_a_completed_task_writes_the_halt_marker(tmp_path):
    record_dir = _record_dir(tmp_path)
    refused = {"committed": False, "refused": "own-hunk-stage-refused", "error": "own-hunk staging refused: x"}
    journal = _journal(tmp_path, [_started("commit"), _result(json.dumps(refused))])
    assert _land(record_dir, journal, status="completed") is not None
    marker = halted_run.read_strict(record_dir)
    assert marker["status"] == "halted" and marker["halted"] == HALT_COMMIT_STAGE
    assert "own-hunk-stage-refused" in marker["reason"]


def test_other_refusal_and_bare_uncommitted_on_a_completed_task_write_no_marker(tmp_path):
    record_dir = _record_dir(tmp_path)
    for reply in ({"committed": False, "refused": "verify-overlay-unverified", "error": "e"}, {"committed": False}):
        journal = _journal(tmp_path, [_started("commit"), _result(reply)])
        assert _land(record_dir, journal, status="completed") is None
    assert not (record_dir / halted_run.HALTED_FILE).exists()


def test_committed_reply_on_a_completed_task_writes_no_marker(tmp_path):
    record_dir = _record_dir(tmp_path)
    journal = _journal(tmp_path, [_started("commit"), _result({"committed": True, "sha": "abc"})])
    assert _land(record_dir, journal, status="completed") is None
