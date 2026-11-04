"""paused_runs: record round-trip, resumed filtering, markers, unsafe ids, resume-call rendering."""

import json

import pytest

from coordinator_core.warm import paused_runs as pr
from coordinator_core.workflow_watch.stall import render_resume_call


def test_round_trip_record_fields(tmp_path):
    path = pr.write_paused(tmp_path, run_id="run-1", script_path="/s/wf.js",
                           args={"a": 1}, task_id="t1", session_id="s1")
    assert path == tmp_path / ".coordinator-local" / "paused-runs" / "run-1.json"
    pending = pr.list_pending(tmp_path)
    assert len(pending) == 1
    rec = pending[0]
    for key in ("run_id", "script_path", "args", "task_id", "session_id", "repo", "paused_at"):
        assert rec[key] is not None
    assert rec["args"] == {"a": 1} and rec["repo"] == str(tmp_path)


def test_mark_resumed_removes_from_pending_and_keeps_record(tmp_path):
    pr.write_paused(tmp_path, run_id="r1", script_path="/s.js", args=None)
    pr.write_paused(tmp_path, run_id="r2", script_path="/s.js", args=None)
    assert pr.mark_resumed(tmp_path, "r1") is True
    assert [r["run_id"] for r in pr.list_pending(tmp_path)] == ["r2"]
    assert pr.read_paused(tmp_path, "r1")["resumed_at"]


def test_mark_resumed_missing_record_is_false(tmp_path):
    assert pr.mark_resumed(tmp_path, "nope") is False


def test_list_pending_empty_and_skips_markers_and_garbage(tmp_path):
    assert pr.list_pending(tmp_path) == []
    pr.request_pause(tmp_path)
    pr.mark_handover_complete(tmp_path)
    (pr.paused_dir(tmp_path) / "bad.json").write_text("{not json", encoding="utf-8")
    pr.write_paused(tmp_path, run_id="ok", script_path=None, args=None)
    assert [r["run_id"] for r in pr.list_pending(tmp_path)] == ["ok"]


def test_markers_are_plain_files_and_clearable(tmp_path):
    assert pr.pause_requested(tmp_path) is None
    p = pr.request_pause(tmp_path, requested_by="publish")
    assert p == pr.paused_dir(tmp_path) / "pause-request.json"
    assert json.loads(p.read_text(encoding="utf-8"))["requested_by"] == "publish"
    assert pr.pause_requested(tmp_path)["requested_by"] == "publish"
    assert pr.clear_pause_request(tmp_path) is True
    assert pr.clear_pause_request(tmp_path) is False
    assert pr.handover_complete(tmp_path) is None
    pr.mark_handover_complete(tmp_path, engine="new")
    assert pr.handover_complete(tmp_path)["engine"] == "new"
    assert pr.clear_handover_complete(tmp_path) is True


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "pause request", "a.json"])
def test_unsafe_run_id_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        pr.write_paused(tmp_path, run_id=bad, script_path=None, args=None)
    assert not pr.paused_dir(tmp_path).exists()


def test_marker_name_cannot_be_a_run_id(tmp_path):
    pr.request_pause(tmp_path)
    for name in ("pause-request", "handover-complete"):
        with pytest.raises(ValueError):
            pr.write_paused(tmp_path, run_id=name, script_path=None, args=None)
    assert pr.pause_requested(tmp_path) is not None


def test_resume_call_renders_from_record(tmp_path):
    pr.write_paused(tmp_path, run_id="r9", script_path="/s/wf.js", args={"k": "v"}, task_id="t9")
    rec = pr.list_pending(tmp_path)[0]
    call = render_resume_call(rec["run_id"], rec["script_path"], rec["args"], rec["task_id"])
    assert 'resumeFromRunId: "r9"' in call and '"/s/wf.js"' in call and call.startswith("TaskStop(")
