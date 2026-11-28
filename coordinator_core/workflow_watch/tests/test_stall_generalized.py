"""Staleness scan, shared resume renderer and stop-marker path, over tmp_path run dirs."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.workflow_watch import stall
from pathlib import Path

NOW = 2_000_000_000.0
OLD = NOW - stall.STALL_BOUND_SECONDS - 600
FRESH = NOW - 30


def _started(agent_id, key=None):
    return {"type": "started", "agentId": agent_id, "label": f"L-{agent_id}",
            "key": key or f"v2:{agent_id}"}


def _run(tmp_path, events, transcripts, journal_mtime: float = OLD) -> str:
    run_dir = tmp_path / "wf_abc"
    run_dir.mkdir()
    journal = run_dir / "journal.jsonl"
    journal.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    os.utime(journal, (journal_mtime, journal_mtime))
    for agent_id, mtime in transcripts.items():
        path = run_dir / f"agent-{agent_id}.jsonl"
        path.write_text('{"type":"assistant"}\n', encoding="utf-8")
        os.utime(path, (mtime, mtime))
    return str(journal)


def test_started_no_result_with_stale_transcript_is_stalled(tmp_path: Path) -> None:
    journal = _run(tmp_path, [_started("a1")], {"a1": OLD})
    found = stall.scan_for_stall(journal, NOW)
    assert found == stall.StalledRun("wf_abc", "a1", "L-a1", NOW - OLD)


def test_fresh_transcript_is_not_stalled(tmp_path: Path) -> None:
    journal = _run(tmp_path, [_started("a1")], {"a1": FRESH})
    assert stall.scan_for_stall(journal, NOW) is None


def test_settled_run_is_not_stalled(tmp_path: Path) -> None:
    done = {"type": "result", "agentId": "a1", "key": "v2:a1", "result": "ok"}
    journal = _run(tmp_path, [_started("a1"), done], {"a1": OLD})
    assert stall.scan_for_stall(journal, NOW) is None


def test_resumed_key_with_fresh_transcript_is_not_stalled(tmp_path: Path) -> None:
    journal = _run(tmp_path, [_started("a1"), _started("a2", key="v2:a1")],
                   {"a1": OLD, "a2": FRESH})
    assert stall.scan_for_stall(journal, NOW) is None


def test_absent_transcript_falls_back_to_journal_mtime(tmp_path: Path) -> None:
    journal = _run(tmp_path, [_started("a1")], {})
    assert stall.scan_for_stall(journal, NOW) is not None
    os.utime(journal, (FRESH, FRESH))
    assert stall.scan_for_stall(journal, NOW) is None


def test_fresh_journal_blocks_a_stale_transcript(tmp_path: Path) -> None:
    journal = _run(tmp_path, [_started("a1")], {"a1": OLD}, journal_mtime=FRESH)
    assert stall.scan_for_stall(journal, NOW) is None


def test_oldest_progress_agent_is_named(tmp_path: Path) -> None:
    journal = _run(tmp_path, [_started("a1"), _started("a2")],
                   {"a1": OLD, "a2": OLD - 500})
    assert stall.scan_for_stall(journal, NOW).agent_id == "a2"


def test_scan_is_fast_and_spawns_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal = _run(tmp_path, [_started("a1")], {"a1": OLD})

    def boom(*args, **kwargs):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    began = time.process_time()
    assert stall.scan_for_stall(journal, NOW) is not None
    assert time.process_time() - began < 0.05


def test_render_matches_precompact_contract() -> None:
    assert stall.render_resume_call("wf_x", "a.mjs", {"repoRoot": "R"}) == (
        'Workflow({scriptPath: "a.mjs", args: {"repoRoot": "R"}, resumeFromRunId: "wf_x"})')
    assert stall.render_resume_call("wf_x", "a.mjs", None) == (
        'Workflow({scriptPath: "a.mjs", resumeFromRunId: "wf_x"})')
    assert stall.render_resume_call("wf_x", None, None) == (
        "Workflow({scriptPath: <unknown -- capture missed it>, resumeFromRunId: \"wf_x\"})")
    assert stall.render_resume_call("bad\nid", "a.mjs", None) == ""
    assert stall.render_resume_call(None, "a.mjs", None) == ""
    assert stall.render_resume_call("wf_x", 'a"b\nc', None).startswith(
        "Workflow({scriptPath: <unknown")


def test_render_with_task_id_prefixes_taskstop() -> None:
    out = stall.render_resume_call("wf_x", "a.mjs", {"k": 1}, task_id="t1")
    assert out == ('TaskStop({"task_id": "t1"}) then '
                   'Workflow({scriptPath: "a.mjs", args: {"k": 1}, resumeFromRunId: "wf_x"})')


def test_stopped_marker_path_builds_and_validates(tmp_path: Path) -> None:
    assert stall.stopped_marker_path(str(tmp_path), "s-1", "t_2") == os.path.join(
        str(tmp_path), "workflow-stopped-s-1-t_2")
    for session, task in (("s/1", "t"), ("s", "../t"), ("", "t"), ("s", "t t")):
        with pytest.raises(ValueError):
            stall.stopped_marker_path(str(tmp_path), session, task)


def _warp_run(root: Path, run_id: str, marker: dict | None) -> str:
    if marker is not None:
        d = root / "state" / "warp" / run_id
        d.mkdir(parents=True)
        (d / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    return str(root / "scratch" / "warp" / f"{run_id}.workflow.mjs")


_NEXT = {"op": "engine-ask", "call": "engine-ask {sizing: state/sizings/x.yaml}"}  # shape of a marker's next_action


def test_returned_halt_names_the_marker_refire_not_a_resume(tmp_path: Path) -> None:
    script = _warp_run(tmp_path, "warp-r1", {"status": "halted", "halted": "gate", "next_action": _NEXT})
    out = stall.render_resume_call("wf_1", script, None, task_id="t1")
    assert out == _NEXT["call"] and "resumeFromRunId" not in out
    assert stall.halted_refire_call(script.replace("/", "\\")) == _NEXT["call"]


def test_returned_halt_without_next_action_names_the_sizing_fire_call(tmp_path: Path) -> None:
    script = _warp_run(tmp_path, "warp-r2", {"status": "halted", "halted": "gate", "sizing_rel": "state/sizings/x.yaml"})
    out = stall.render_resume_call("wf_1", script, None)
    assert "state/sizings/x.yaml" in out and "resumeFromRunId" not in out


@pytest.mark.parametrize("marker", [
    {"status": "halted", "halted": "usage_limit", "next_action": _NEXT},
    {"status": "resumed", "halted": "gate", "next_action": _NEXT},
    None,
])
def test_usage_limit_resumed_and_unmarked_runs_keep_the_resume(tmp_path: Path, marker) -> None:
    script = _warp_run(tmp_path, "warp-r3", marker)
    out = stall.render_resume_call("wf_1", script, None)
    assert out == f'Workflow({{scriptPath: {json.dumps(script)}, resumeFromRunId: "wf_1"}})'
