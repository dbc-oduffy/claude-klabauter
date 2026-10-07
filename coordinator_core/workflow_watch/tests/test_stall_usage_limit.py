"""Usage-limit stall scan, over tmp_path run dirs built from the recorded hit's shape."""

from __future__ import annotations

import json
import time
from pathlib import Path

from coordinator_core.workflow_watch import stall

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "usage_limit_hit.json").read_text(encoding="utf-8"))
RESETS_AT = FIXTURE["quotaLimits"]["resetsAt"]
NOW = RESETS_AT - 3600.0

RATE_LIMIT_TURN = {
    "type": "assistant",
    "timestamp": "2026-10-06T15:10:28Z",
    "message": {"model": "<synthetic>", "stop_reason": "stop_sequence",
                "content": [{"type": "text",
                             "text": "You've hit your session limit · resets 7:40pm (Europe/London)"}]},
    "isApiErrorMessage": True,
    **FIXTURE,
}
NORMAL_TURN = {"type": "assistant", "timestamp": "2026-10-06T15:09:00Z",
               "message": {"content": [{"type": "text", "text": "running tests"}]}}


def _run(tmp_path, events, transcripts, subdir=""):
    run_dir = tmp_path / "wf_e039085a-f70"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    target = run_dir / subdir if subdir else run_dir
    target.mkdir(exist_ok=True)
    for agent_id, turns in transcripts.items():
        (target / f"agent-{agent_id}.jsonl").write_text(
            "\n".join(json.dumps(t) for t in turns) + "\n", encoding="utf-8")
    return str(run_dir / "journal.jsonl")


def _started(agent_id, label="test:terminal"):
    return {"type": "started", "agentId": agent_id, "label": label, "key": f"v2:{agent_id}"}


def test_stalled_agent_with_rate_limit_tail_yields_halt(tmp_path):
    journal = _run(tmp_path, [_started("a99c")], {"a99c": [NORMAL_TURN, RATE_LIMIT_TURN]})
    began = time.process_time()
    halt = stall.scan_for_usage_limit(journal, NOW)
    assert time.process_time() - began < 0.2
    assert halt is not None
    record = halt.as_record("wf.js")
    assert record["halted_by"] == "usage_limit"
    assert record["resets_at"] == RESETS_AT
    assert record["resets_at_iso"] == "2026-10-06T18:40:00Z"
    assert record["agent_label"] == "test:terminal"
    assert record["run_id"] == "wf_e039085a-f70"
    assert record["resume"] == ("TaskStop the task, then Workflow({scriptPath: 'wf.js', "
                                "resumeFromRunId: 'wf_e039085a-f70'})")
    assert halt.line("wf.js").startswith("halted: ")


def test_transcript_under_subagents_dir_is_found(tmp_path):
    journal = _run(tmp_path, [_started("a99c")], {"a99c": [RATE_LIMIT_TURN]}, subdir="subagents")
    assert stall.scan_for_usage_limit(journal, NOW) is not None


def test_slow_agent_without_error_yields_no_halt(tmp_path):
    journal = _run(tmp_path, [_started("a99c")], {"a99c": [NORMAL_TURN]})
    assert stall.scan_for_usage_limit(journal, NOW) is None


def test_completed_agent_is_ignored(tmp_path):
    events = [_started("a99c"), {"type": "result", "agentId": "a99c", "key": "v2:a99c",
                                 "result": "ok"}]
    journal = _run(tmp_path, events, {"a99c": [RATE_LIMIT_TURN]})
    assert stall.scan_for_usage_limit(journal, NOW) is None


def test_lifted_limit_is_ignored(tmp_path):
    journal = _run(tmp_path, [_started("a99c")], {"a99c": [RATE_LIMIT_TURN]})
    assert stall.scan_for_usage_limit(journal, RESETS_AT + 1) is None


def test_resumed_key_with_result_settles_the_old_agent(tmp_path):
    old = _started("a99c")
    new = {"type": "started", "agentId": "ad71", "label": "test:terminal", "key": "v2:a99c"}
    done = {"type": "result", "agentId": "ad71", "key": "v2:a99c", "result": "pass"}
    journal = _run(tmp_path, [old, new, done], {"a99c": [RATE_LIMIT_TURN]})
    assert stall.scan_for_usage_limit(journal, NOW) is None


def test_watch_exits_halted_with_runnable_resume_line(tmp_path, capsys, monkeypatch):
    import types

    from coordinator_core import workflow_watch
    from coordinator_core.workflow_watch import _watch
    monkeypatch.setattr(workflow_watch, "time", types.SimpleNamespace(
        time=lambda: NOW, monotonic=time.monotonic, sleep=time.sleep))
    run_dir = tmp_path / "runs"
    run_dir.mkdir()
    journal = _run(run_dir, [_started("a99c")], {"a99c": [NORMAL_TURN, RATE_LIMIT_TURN]})
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    code = _watch(str(transcript), journal, "tid-1", 0.01, 5.0, script_path="wf.js")
    assert code == stall.EXIT_HALTED_USAGE_LIMIT
    out = capsys.readouterr().out.strip()
    assert out.startswith("halted: ")
    record = json.loads(out[len("halted: "):])
    assert record["halted_by"] == "usage_limit"
    assert "scriptPath: 'wf.js'" in record["resume"]


def test_journal_tail_cap_scans_inside_and_skips_outside(tmp_path):
    pad = {"type": "note", "pad": "x" * 1000}
    padding = [pad] * ((stall.JOURNAL_TAIL_BYTES // 1000) + 50)
    inside = _run(tmp_path, padding + [_started("a99c")], {"a99c": [RATE_LIMIT_TURN]})
    assert stall.scan_for_usage_limit(inside, NOW) is not None
    outside_dir = tmp_path / "other"
    outside_dir.mkdir()
    outside = _run(outside_dir, [_started("a99c")] + padding, {"a99c": [RATE_LIMIT_TURN]})
    assert stall.scan_for_usage_limit(outside, NOW) is None


def test_rejected_quota_without_rate_limit_error_does_not_halt(tmp_path):
    no_error = {k: v for k, v in RATE_LIMIT_TURN.items() if k not in ("error", "apiErrorStatus")}
    journal = _run(tmp_path, [_started("a99c")], {"a99c": [no_error]})
    assert stall.scan_for_usage_limit(journal, NOW) is None
    wrong_status = {**RATE_LIMIT_TURN, "apiErrorStatus": 500}
    other = tmp_path / "other"
    other.mkdir()
    journal = _run(other, [_started("a99c")], {"a99c": [wrong_status]})
    assert stall.scan_for_usage_limit(journal, NOW) is None
