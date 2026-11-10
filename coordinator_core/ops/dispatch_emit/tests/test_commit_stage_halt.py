"""A run whose commit-stage leg was cancelled or failed leaves a `commit_stage` halt marker."""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core import workflow_watch
from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, halted_run_landed
from coordinator_core.ops.dispatch_emit.commit_stage_halt import (
    HALT_COMMIT_STAGE,
    commit_leg_outcome,
    halt_run_on_commit_stage_end,
)

SIZING = "state/sizings/2026-11/x.yaml"
RUN_ID = "ask-20261110T071829-ab7f5a"
SCRIPT = "scripts/wf.js"


def _started(label, agent="a1", key="k1"):
    return {"type": "started", "agentId": agent, "label": label, "key": key}


def _journal(tmp_path, events):
    run_dir = tmp_path / "runs" / "wf_abc"
    run_dir.mkdir(parents=True, exist_ok=True)
    journal = run_dir / "journal.jsonl"
    journal.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return str(journal)


def _record_dir(tmp_path):
    record_dir = ask_contract.record_dir(tmp_path, RUN_ID)
    record_dir.mkdir(parents=True, exist_ok=True)
    submitted = tmp_path / "scratch" / "warp" / RUN_ID
    submitted.mkdir(parents=True, exist_ok=True)
    (submitted / "submission.json").write_text(json.dumps({
        "run_id": RUN_ID, "script_path": SCRIPT, "sizing_rel": SIZING, "submitted_at": 1.0,
    }), encoding="utf-8")
    return record_dir


def _land(record_dir, journal, status="failed", launched_at=1.0):
    return halt_run_on_commit_stage_end(
        record_dir, journal, terminal_status=status, sizing_rel=SIZING, launched_at=launched_at,
    )


def test_unsettled_commit_leg_on_a_failed_task_halts_at_commit(tmp_path):
    record_dir = _record_dir(tmp_path)
    journal = _journal(tmp_path, [_started("commit")])
    assert _land(record_dir, journal) is not None
    marker = halted_run.read_strict(record_dir)
    assert marker["status"] == "halted" and marker["halted"] == HALT_COMMIT_STAGE
    assert marker["sizing_rel"] == SIZING
    assert "commit leg unsettled" in marker["reason"] and "failed" in marker["reason"]


def test_failed_commit_leg_carries_its_error_in_the_reason(tmp_path):
    record_dir = _record_dir(tmp_path)
    journal = _journal(tmp_path, [
        _started("halt-commit"), {"type": "failed", "agentId": "a1", "key": "k1", "error": "approval prompt cancelled"},
    ])
    assert _land(record_dir, journal, status="killed") is not None
    assert "approval prompt cancelled" in halted_run.read_strict(record_dir)["reason"]


def test_committed_reply_writes_no_marker(tmp_path):
    record_dir = _record_dir(tmp_path)
    journal = _journal(tmp_path, [
        _started("commit"), {"type": "result", "agentId": "a1", "key": "k1", "result": {"committed": True, "sha": "abc"}},
    ])
    assert _land(record_dir, journal) is None
    assert not (record_dir / "halted.json").exists()


def test_already_landed_json_text_reply_writes_no_marker(tmp_path):
    record_dir = _record_dir(tmp_path)
    journal = _journal(tmp_path, [
        _started("commit"),
        {"type": "result", "agentId": "a1", "key": "k1", "result": json.dumps({"already_landed": True})},
    ])
    assert _land(record_dir, journal) is None


def test_uncommitted_reply_halts_only_when_the_task_did_not_complete(tmp_path):
    record_dir = _record_dir(tmp_path)
    journal = _journal(tmp_path, [
        _started("commit"), {"type": "result", "agentId": "a1", "key": "k1", "result": {"committed": False}},
    ])
    assert _land(record_dir, journal, status="completed") is None
    assert _land(record_dir, journal, status="killed") is not None


def test_run_with_no_commit_stage_leg_writes_no_marker(tmp_path):
    record_dir = _record_dir(tmp_path)
    journal = _journal(tmp_path, [_started("commit:wave-1"), _started("review", agent="a2", key="k2")])
    assert _land(record_dir, journal) is None
    assert not (record_dir / "halted.json").exists()


def test_a_retried_commit_leg_that_landed_wins_over_its_cancelled_first_try(tmp_path):
    journal = _journal(tmp_path, [
        _started("commit", agent="a1", key="k1"), {"type": "failed", "agentId": "a1", "key": "k1"},
        _started("commit", agent="a2", key="k2"),
        {"type": "result", "agentId": "a2", "key": "k2", "result": {"committed": True}},
    ])
    assert commit_leg_outcome(open(journal, encoding="utf-8").read())[1] == "committed"


def test_existing_fresh_marker_is_not_overwritten(tmp_path):
    record_dir = _record_dir(tmp_path)
    halted_run.write_halt_marker(record_dir, halted="split", sizing_rel=SIZING)
    journal = _journal(tmp_path, [_started("commit")])
    assert _land(record_dir, journal, launched_at=1.0) is None
    assert halted_run.read_strict(record_dir)["halted"] == "split"


def test_stale_marker_from_before_the_launch_is_replaced(tmp_path):
    record_dir = _record_dir(tmp_path)
    halted_run.write_halt_marker(record_dir, halted="split", sizing_rel=SIZING)
    old = os.stat(record_dir / "halted.json").st_mtime - 100
    os.utime(record_dir / "halted.json", (old, old))
    journal = _journal(tmp_path, [_started("commit")])
    assert _land(record_dir, journal, launched_at=old + 50) is not None
    assert halted_run.read_strict(record_dir)["halted"] == HALT_COMMIT_STAGE


def test_landed_marker_is_never_rewritten(tmp_path):
    record_dir = _record_dir(tmp_path)
    halted_run.write_halt_marker(record_dir, halted="split", sizing_rel=SIZING)
    assert halted_run.mark_landed(record_dir, "a" * 40)
    old = os.stat(record_dir / "halted.json").st_mtime - 100
    os.utime(record_dir / "halted.json", (old, old))
    journal = _journal(tmp_path, [_started("commit")])
    assert _land(record_dir, journal, launched_at=old + 50) is None
    assert halted_run.read_strict(record_dir)["status"] == halted_run.STATUS_LANDED


def test_writer_never_raises(tmp_path, monkeypatch):
    record_dir = _record_dir(tmp_path)
    journal = _journal(tmp_path, [_started("commit")])
    monkeypatch.setattr(halted_run, "write_halt_marker", lambda *a, **k: 1 / 0)
    assert _land(record_dir, journal) is None
    assert _land(record_dir, str(tmp_path / "missing.jsonl")) is None


@pytest.mark.parametrize("status", ["failed", "killed", "stopped"])
def test_watch_at_default_arguments_leaves_a_marker_the_halted_close_accepts(tmp_path, monkeypatch, status):
    record_dir = _record_dir(tmp_path)
    journal = _journal(tmp_path, [_started("commit")])
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        f"<task-notification><task-id>task1</task-id><status>{status}</status></task-notification>",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert workflow_watch._watch(str(transcript), journal, "task1", 0.01, 5.0, script_path=SCRIPT) == 0
    marker = halted_run.read_strict(record_dir)
    assert marker["halted"] == HALT_COMMIT_STAGE and marker["sizing_rel"] == SIZING
    out = halted_run_landed._record_solo(tmp_path, RUN_ID, ["abc1234"])
    assert "never ran" not in json.dumps(out)
