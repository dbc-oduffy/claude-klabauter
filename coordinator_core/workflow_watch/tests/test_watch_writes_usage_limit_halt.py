"""_watch lands halted.json for the submitted ask run when it detects the usage limit."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core import workflow_watch
from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
from coordinator_core.ops.dispatch_emit.halt_refire import HALT_USAGE_LIMIT
from coordinator_core.workflow_watch import EXIT_HALTED_USAGE_LIMIT

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "usage_limit_hit.json").read_text(encoding="utf-8"))
RATE_LIMIT_TURN = {"type": "assistant", "message": {"content": [{"type": "text", "text": "limit"}]},
                   "isApiErrorMessage": True, **FIXTURE}
SIZING = "state/sizings/2026-10-29-x.yaml"
RUN_ID = "ask-20261029T100000-abcdef"
SCRIPT = "scripts/wf.js"


def _setup(tmp_path, script_in_submission=SCRIPT):
    run_dir = tmp_path / "runs" / "wf_abc"
    run_dir.mkdir(parents=True)
    journal = run_dir / "journal.jsonl"
    journal.write_text(json.dumps({"type": "started", "agentId": "a1", "label": "leg:a1", "key": "v2:a1"}) + "\n",
                       encoding="utf-8")
    (run_dir / "agent-a1.jsonl").write_text(json.dumps(RATE_LIMIT_TURN) + "\n", encoding="utf-8")
    record_dir = tmp_path / "scratch" / "warp" / RUN_ID
    record_dir.mkdir(parents=True)
    (record_dir / "submission.json").write_text(json.dumps({
        "run_id": RUN_ID, "script_path": script_in_submission, "sizing_rel": SIZING, "submitted_at": 1.0,
    }), encoding="utf-8")
    return ask_contract.record_dir(tmp_path, RUN_ID), str(journal)


def test_halt_writes_marker_for_the_submitted_run(tmp_path):
    record_dir, journal = _setup(tmp_path)
    workflow_watch._write_usage_limit_halt(journal, SCRIPT, root=tmp_path)
    marker = halted_run.read_strict(record_dir)
    assert marker["halted"] == HALT_USAGE_LIMIT and marker["sizing_rel"] == SIZING


def test_no_matching_submission_writes_nothing(tmp_path):
    record_dir, journal = _setup(tmp_path, script_in_submission="scripts/other.js")
    workflow_watch._write_usage_limit_halt(journal, SCRIPT, root=tmp_path)
    assert not (record_dir / "halted.json").exists()


def test_no_script_path_writes_nothing(tmp_path):
    record_dir, journal = _setup(tmp_path)
    workflow_watch._write_usage_limit_halt(journal, None, root=tmp_path)
    assert not (record_dir / "halted.json").exists()


def test_watch_writes_marker_and_exits_halted(tmp_path, monkeypatch):
    record_dir, journal = _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(workflow_watch.time, "time", lambda: FIXTURE["quotaLimits"]["resetsAt"] - 3600.0)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    code = workflow_watch._watch(str(transcript), journal, "task1", 0.01, 5.0, script_path=SCRIPT)
    assert code == EXIT_HALTED_USAGE_LIMIT
    assert halted_run.read_strict(record_dir)["halted"] == HALT_USAGE_LIMIT


def test_write_failure_never_changes_exit_code(tmp_path, monkeypatch):
    record_dir, journal = _setup(tmp_path)
    monkeypatch.setattr(workflow_watch, "_submitted_run", lambda *a, **k: 1 / 0)
    workflow_watch._write_usage_limit_halt(journal, SCRIPT, root=tmp_path)
    assert not (record_dir / "halted.json").exists()
