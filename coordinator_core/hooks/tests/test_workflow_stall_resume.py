"""workflow_stall_resume_text names a stalled run's exact resume call."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.hooks import postuse_advisory_dispatch as mod
from coordinator_core.workflow_watch import stall

SID = "sess-1"
NOW = 2_000_000_000.0


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(mod, "_managed_tmpdir", lambda: str(d))
    return d


def _age(path, seconds):
    t = NOW - seconds
    os.utime(path, (t, t))


def _run(tmp_path, name, *, settled=False, transcript_age=7200):
    run_dir = tmp_path / name
    (run_dir / "subagents").mkdir(parents=True)
    events = [{"type": "started", "agentId": "a1", "key": "k1", "label": "scout"}]
    if settled:
        events.append({"type": "result", "agentId": "a1", "key": "k1"})
    journal = run_dir / "journal.jsonl"
    journal.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    transcript = run_dir / "subagents" / "agent-a1.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _age(journal, 7200)
    _age(transcript, transcript_age)
    return run_dir


def _record(tdir, run_dir, *, session=SID, task="t1", run="wf_1", fired_at=NOW - 3600, args=None):
    rec = {
        "run_id": run, "scriptPath": "scripts/x.workflow.mjs",
        "args": {"repoRoot": str(tdir)} if args is None else args,
        "fired_at": fired_at, "session_id": session, "task_id": task,
        "tool_use_id": "tu1", "transcript_dir": str(run_dir),
    }
    (tdir / f"workflow-run-{session}-{task}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def test_stalled_run_names_original_call(tmp_path, tdir):
    rec = _record(tdir, _run(tmp_path, "r1"))
    text = mod.workflow_stall_resume_text(SID, "", NOW)
    assert stall.render_resume_call("wf_1", rec["scriptPath"], rec["args"], "t1") in text
    assert "wf_1" in text and "scout" in text and "t1" in text
    assert json.dumps(rec["args"]) in text


def test_settled_run_yields_nothing(tmp_path, tdir):
    _record(tdir, _run(tmp_path, "r1", settled=True))
    assert mod.workflow_stall_resume_text(SID, "", NOW) == ""


def test_fresh_transcript_yields_nothing(tmp_path, tdir):
    _record(tdir, _run(tmp_path, "r1", transcript_age=10))
    assert mod.workflow_stall_resume_text(SID, "", NOW) == ""


def test_stopped_marker_yields_nothing(tmp_path, tdir):
    _record(tdir, _run(tmp_path, "r1"))
    open(stall.stopped_marker_path(str(tdir), SID, "t1"), "w", encoding="utf-8").close()
    assert mod.workflow_stall_resume_text(SID, "", NOW) == ""


def _transcript(tmp_path, text):
    p = tmp_path / "session.jsonl"
    p.write_text(text + "\n", encoding="utf-8")
    return str(p)


def test_reaped_task_reported_once_then_silent(tmp_path, tdir):
    _record(tdir, _run(tmp_path, "r1"))
    tp = _transcript(tmp_path, json.dumps({"content": "Error: No task found with ID: t1"}))
    first = mod.workflow_stall_resume_text(SID, tp, NOW)
    assert "abandoned" in first and "wf_1" in first
    assert mod.workflow_stall_resume_text(SID, tp, NOW) == ""


def test_other_tasks_no_task_found_leaves_live_run_advised(tmp_path, tdir):
    _record(tdir, _run(tmp_path, "r1"))
    tp = _transcript(tmp_path, json.dumps({"content": "Error: No task found with ID: zzz"}))
    text = mod.workflow_stall_resume_text(SID, tp, NOW)
    assert "abandoned" not in text and 'TaskStop({"task_id": "t1"})' in text


def test_another_sessions_record_yields_nothing(tmp_path, tdir):
    _record(tdir, _run(tmp_path, "r1"), session="other")
    assert mod.workflow_stall_resume_text(SID, "", NOW) == ""


def test_record_past_horizon_yields_nothing(tmp_path, tdir):
    _record(tdir, _run(tmp_path, "r1"), fired_at=NOW - stall.STALL_HORIZON_SECONDS - 1)
    assert mod.workflow_stall_resume_text(SID, "", NOW) == ""


def test_unsafe_session_id_yields_nothing(tdir):
    assert mod.workflow_stall_resume_text("../x", "", NOW) == ""


def test_newest_record_per_run_wins(tmp_path, tdir):
    run_dir = _run(tmp_path, "r1")
    _record(tdir, run_dir, task="old", fired_at=NOW - 5000)
    _record(tdir, run_dir, task="new", fired_at=NOW - 4000)
    open(stall.stopped_marker_path(str(tdir), SID, "old"), "w", encoding="utf-8").close()
    text = mod.workflow_stall_resume_text(SID, "", NOW)
    assert 'TaskStop({"task_id": "new"})' in text


def test_twenty_records_one_stalled_within_budget_no_subprocess(tmp_path, tdir, monkeypatch):
    for i in range(19):
        _record(tdir, _run(tmp_path, f"ok{i}", settled=True), task=f"o{i}", run=f"wf_ok{i}")
    _record(tdir, _run(tmp_path, "stuck"), task="tS", run="wf_stuck")

    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    start = time.process_time()
    text = mod.workflow_stall_resume_text(SID, "", NOW)
    elapsed = time.process_time() - start
    assert "wf_stuck" in text
    assert elapsed < 0.05


def test_workflow_stall_resume_text_contract_for_doe_stop_shim() -> None:
    # External consumer: DoE coordinator/hooks/scripts/stop-workflow-stall.py (DoE commit 90ffb846d7)
    # imports this symbol from this exact module and fails open silently if it moves or renames.
    import importlib
    import inspect

    mod = importlib.import_module("coordinator_core.hooks.postuse_advisory_dispatch")
    fn = getattr(mod, "workflow_stall_resume_text")
    assert list(inspect.signature(fn).parameters) == ["session_id", "transcript_path", "now"]
