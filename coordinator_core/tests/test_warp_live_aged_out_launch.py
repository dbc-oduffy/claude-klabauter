"""warp_live: a token whose launch record aged out, or whose task never started, frees a refire."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import run_token

SIZING = "state/sizings/mine.yaml"
SCRIPT = "scratch/warp/x.mjs"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    return managed, legacy


def _launch_record(directory, session, task):
    (directory / f"workflow-run-{session}-{task}.json").write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": 1.0}), encoding="utf-8"
    )


def _launch_line(tool_use_id, task):
    obj = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
        "toolUseResult": {"status": "async_launched", "taskType": "local_workflow", "taskId": task},
    }
    return json.dumps(obj) + "\n"


def _terminal(task):
    return (
        f"<task-notification><task-id>{task}</task-id><status>completed</status>"
        "<result>halted: ledger-check</result></task-notification>\n"
    )


def _gone(task):
    return f"TaskStop failed: No task found with ID: {task}\n"


def _setup(repo, tmp_path, body):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(body, encoding="utf-8")
    warp_live.record_submission(
        repo, "r1", session_id="s1", transcript_path=str(transcript), script_path=SCRIPT,
        sizing_rel=SIZING, tool_use_id="tu1",
    )
    return str(transcript)


def _refire_allowed(repo, transcript):
    return warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=transcript) is None


def test_launch_record_removed_but_transcript_shows_the_task_terminal(repo, dirs, tmp_path):
    transcript = _setup(repo, tmp_path, _launch_line("tu1", "w1") + _terminal("w1"))
    assert _refire_allowed(repo, transcript)
    assert not run_token.is_live(repo, "r1")


def test_launch_record_removed_and_task_still_running_stays_live(repo, dirs, tmp_path):
    transcript = _setup(repo, tmp_path, _launch_line("tu1", "w1") + _terminal("w2"))
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=transcript) == "r1"
    assert run_token.is_live(repo, "r1")


def test_launch_record_removed_and_launch_absent_from_transcript_waits_for_grace(repo, dirs, tmp_path):
    transcript = _setup(repo, tmp_path, "")
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=transcript) == "r1"


def test_task_the_harness_never_started_frees_the_refire(repo, dirs, tmp_path):
    transcript = _setup(repo, tmp_path, _gone("w1"))
    _launch_record(dirs[0], "s1", "w1")
    assert _refire_allowed(repo, transcript)
    assert not run_token.is_live(repo, "r1")


def test_a_gone_report_for_another_task_leaves_the_run_live(repo, dirs, tmp_path):
    transcript = _setup(repo, tmp_path, _gone("w2"))
    _launch_record(dirs[0], "s1", "w1")
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=transcript) == "r1"
    assert run_token.is_live(repo, "r1")


def test_live_run_for_sizing_stays_under_50ms(repo, dirs, tmp_path):
    transcript = _setup(repo, tmp_path, _launch_line("tu1", "w1") + _terminal("w1"))
    start = time.process_time()
    warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=transcript)
    assert time.process_time() - start < 0.05
