"""Workflow run-record capture pairs each launch with its own tool_use by id."""

from __future__ import annotations

import json
import os

from coordinator_core.hooks import postuse_advisory_dispatch as mod

SID = "sess-1"


def _use(tool_use_id, script, args=None, resume=None):
    inp = {"scriptPath": script, "args": args or {}}
    if resume:
        inp["resumeFromRunId"] = resume
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": tool_use_id, "name": "Workflow", "input": inp}
        ]},
    }


def _launch(tool_use_id, task_id, run_id, tdir="runs\\x"):
    return {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}
        ]},
        "toolUseResult": {
            "status": "async_launched", "taskId": task_id, "taskType": "local_workflow",
            "runId": run_id, "transcriptDir": tdir,
        },
    }


def _error(tool_use_id):
    return {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": "refused", "is_error": True}
        ]},
    }


def _transcript(tmp_path, entries):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return str(path)


def _tmpdir(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir(exist_ok=True)
    return str(d)


def _rec(tmpdir, name):
    with open(os.path.join(tmpdir, f"workflow-run-{SID}-{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _exists(tmpdir, name):
    return os.path.exists(os.path.join(tmpdir, f"workflow-run-{SID}-{name}.json"))


def test_previous_launch_is_not_attributed_to_the_newest_tool_use(tmp_path):
    tdir = _tmpdir(tmp_path)
    t = _transcript(tmp_path, [
        _use("toolu_A", "scriptA", {"k": "a"}),
        _launch("toolu_A", "tA", "rA"),
        _use("toolu_B", "scriptB", {"k": "b"}),
    ])
    mod.resolve_workflow_run_records(SID, t, tdir)
    a = _rec(tdir, "tA")
    assert (a["run_id"], a["scriptPath"], a["args"], a["tool_use_id"]) == ("rA", "scriptA", {"k": "a"}, "toolu_A")
    assert a["task_id"] == "tA" and a["transcript_dir"] == "runs\\x" and a["session_id"] == SID
    b = _rec(tdir, "toolu_B")
    assert b["run_id"] is None and b["pending"] is True and b["scriptPath"] == "scriptB"
    assert not _exists(tdir, "toolu_A")


def test_pending_record_resolves_and_is_removed_on_a_later_pass(tmp_path):
    tdir = _tmpdir(tmp_path)
    entries = [_use("toolu_B", "scriptB", {"k": "b"})]
    mod.resolve_workflow_run_records(SID, _transcript(tmp_path, entries), tdir)
    assert _exists(tdir, "toolu_B")
    entries.append(_launch("toolu_B", "tB", "rB"))
    written = mod.resolve_workflow_run_records(SID, _transcript(tmp_path, entries), tdir)
    assert [r["task_id"] for r in written] == ["tB"]
    assert _rec(tdir, "tB")["scriptPath"] == "scriptB"
    assert not _exists(tdir, "toolu_B")


def test_resolution_when_tool_use_fell_out_of_the_tail_uses_the_pending_record(tmp_path):
    tdir = _tmpdir(tmp_path)
    mod.resolve_workflow_run_records(
        SID, _transcript(tmp_path, [_use("toolu_B", "scriptB", {"k": "b"})]), tdir
    )
    mod.resolve_workflow_run_records(
        SID, _transcript(tmp_path, [_launch("toolu_B", "tB", "rB")]), tdir
    )
    assert _rec(tdir, "tB")["args"] == {"k": "b"}
    assert not _exists(tdir, "toolu_B")


def test_error_result_removes_the_pending_record_only(tmp_path):
    tdir = _tmpdir(tmp_path)
    mod.resolve_workflow_run_records(
        SID, _transcript(tmp_path, [_use("toolu_B", "scriptB")]), tdir
    )
    assert _exists(tdir, "toolu_B")
    mod.resolve_workflow_run_records(
        SID, _transcript(tmp_path, [_use("toolu_B", "scriptB"), _error("toolu_B")]), tdir
    )
    assert sorted(os.listdir(tdir)) == []


def test_two_parallel_calls_in_one_message_pair_by_id(tmp_path):
    tdir = _tmpdir(tmp_path)
    t = _transcript(tmp_path, [
        {"type": "assistant", "message": {"content": [
            _use("toolu_A", "scriptA")["message"]["content"][0],
            _use("toolu_B", "scriptB")["message"]["content"][0],
        ]}},
        _launch("toolu_B", "tB", "rB"),
        _launch("toolu_A", "tA", "rA"),
    ])
    mod.resolve_workflow_run_records(SID, t, tdir)
    assert (_rec(tdir, "tA")["scriptPath"], _rec(tdir, "tA")["run_id"]) == ("scriptA", "rA")
    assert (_rec(tdir, "tB")["scriptPath"], _rec(tdir, "tB")["run_id"]) == ("scriptB", "rB")


def test_resume_call_records_the_reused_run_id_under_its_new_task_id(tmp_path):
    tdir = _tmpdir(tmp_path)
    t = _transcript(tmp_path, [
        _use("toolu_R", "scriptA", {"k": 1}, resume="rA"),
        _launch("toolu_R", "tNew", "rA"),
    ])
    mod.resolve_workflow_run_records(SID, t, tdir)
    r = _rec(tdir, "tNew")
    assert r["run_id"] == "rA" and r["task_id"] == "tNew" and r["args"] == {"k": 1}


def test_correct_record_replaces_a_skewed_legacy_one_idempotently(tmp_path):
    tdir = _tmpdir(tmp_path)
    legacy = os.path.join(tdir, f"workflow-run-{SID}-tA.json")
    with open(legacy, "w", encoding="utf-8") as fh:
        json.dump({"run_id": "wrong", "scriptPath": "scriptB"}, fh)
    t = _transcript(tmp_path, [_use("toolu_A", "scriptA"), _launch("toolu_A", "tA", "rA")])
    mod.resolve_workflow_run_records(SID, t, tdir)
    mod.resolve_workflow_run_records(SID, t, tdir)
    assert (_rec(tdir, "tA")["run_id"], _rec(tdir, "tA")["scriptPath"]) == ("rA", "scriptA")


def test_reprocessing_a_launch_keeps_its_original_fired_at(tmp_path):
    tdir = _tmpdir(tmp_path)
    t = _transcript(tmp_path, [_use("toolu_A", "scriptA"), _launch("toolu_A", "tA", "rA")])
    mod.resolve_workflow_run_records(SID, t, tdir)
    path = os.path.join(tdir, f"workflow-run-{SID}-tA.json")
    rec = _rec(tdir, "tA")
    rec["fired_at"] = 1000
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    mod.resolve_workflow_run_records(SID, t, tdir)
    assert _rec(tdir, "tA")["fired_at"] == 1000


def test_unsafe_ids_never_reach_a_path(tmp_path):
    tdir = _tmpdir(tmp_path)
    t = _transcript(tmp_path, [
        _use("toolu_A", "scriptA"), _launch("toolu_A", "../evil", "rA"),
        _use("../x", "scriptX"),
    ])
    mod.resolve_workflow_run_records(SID, t, tdir)
    assert not any("evil" in n or ".." in n for n in os.listdir(tdir))
    assert not (tmp_path / "evil").exists()


def test_capture_leg_gates_on_tool_name(tmp_path, monkeypatch):
    tdir = _tmpdir(tmp_path)
    monkeypatch.setattr(mod, "_managed_tmpdir", lambda: tdir)
    t = _transcript(tmp_path, [_use("toolu_A", "scriptA")])
    mod._capture_workflow_run_record_sync(SID, t, "Write")
    assert os.listdir(tdir) == []
    mod._capture_workflow_run_record_sync(SID, t, "Workflow")
    assert _exists(tdir, "toolu_A")
