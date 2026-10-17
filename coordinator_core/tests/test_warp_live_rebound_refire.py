"""warp_live: a rebound re-fire is never read ended by an earlier launch of the same script."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import run_token

SIZING = "state/sizings/mine.yaml"
SCRIPT = "scratch/warp/run.workflow.mjs"
OLD_TASK = "w11old"
NEW_TASK = "wknew"
NEW_TU = "tu-new"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def managed(tmp_path, monkeypatch):
    directory = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    directory.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: directory)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    return directory


def _record(directory, name, **fields):
    body = {"scriptPath": SCRIPT, **fields}
    (directory / f"workflow-run-s1-{name}.json").write_text(json.dumps(body), encoding="utf-8")


def _terminal(task):
    return (
        f"<task-notification><task-id>{task}</task-id><status>completed</status>"
        "<result>halted: ledger-check</result></task-notification>\n"
    )


def _launch_line(tool_use_id, task):
    obj = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]},
        "toolUseResult": {"status": "async_launched", "taskType": "local_workflow", "taskId": task},
    }
    return json.dumps(obj) + "\n"


def _setup(repo, tmp_path, managed, *, tool_use_id=NEW_TU, transcript_body=None):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    minted = time.time()
    _record(managed, OLD_TASK, fired_at=int(minted) - 600, tool_use_id="tu-old", task_id=OLD_TASK)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        _terminal(OLD_TASK) if transcript_body is None else transcript_body, encoding="utf-8"
    )
    warp_live.record_submission(
        repo, "r1", session_id="s1", transcript_path=str(transcript), script_path=SCRIPT,
        sizing_rel=SIZING, tool_use_id=tool_use_id,
    )
    return transcript


def _live_after(repo, transcript):
    by_sizing = warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=str(transcript))
    by_session = warp_live.live_run_for_session(repo, "s1", transcript_path=str(transcript))
    return by_sizing, by_session


def test_an_earlier_launch_terminal_never_consumes_the_rebound_token(repo, managed, tmp_path):
    transcript = _setup(repo, tmp_path, managed)
    assert _live_after(repo, transcript) == ("r1", "r1")
    assert run_token.live_records(repo)


def test_the_run_is_consumed_once_its_own_launch_is_captured_and_terminal(repo, managed, tmp_path):
    transcript = _setup(repo, tmp_path, managed)
    _record(managed, NEW_TASK, fired_at=int(time.time()), tool_use_id=NEW_TU, task_id=NEW_TASK)
    transcript.write_text(_terminal(OLD_TASK) + _terminal(NEW_TASK), encoding="utf-8")
    assert _live_after(repo, transcript) == (None, None)
    assert not list(run_token.live_records(repo))


def test_an_uncaptured_launch_ends_on_the_transcript_launch_result(repo, managed, tmp_path):
    body = _terminal(OLD_TASK) + _launch_line(NEW_TU, NEW_TASK) + _terminal(NEW_TASK)
    transcript = _setup(repo, tmp_path, managed, transcript_body=body)
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=str(transcript)) is None


def test_a_pending_record_is_not_taken_as_an_earlier_launchs_task(repo, managed, tmp_path):
    transcript = _setup(repo, tmp_path, managed, transcript_body=_terminal(NEW_TU))
    _record(managed, NEW_TU, pending=True, run_id=None, fired_at=int(time.time()), tool_use_id=NEW_TU)
    assert _live_after(repo, transcript) == ("r1", "r1")


def test_without_a_submitted_tool_use_id_a_launch_before_the_mint_is_not_this_runs(repo, managed, tmp_path):
    transcript = _setup(repo, tmp_path, managed, tool_use_id=None)
    assert _live_after(repo, transcript) == ("r1", "r1")


def test_without_a_submitted_tool_use_id_a_launch_in_the_mints_own_second_counts(repo, managed, tmp_path):
    transcript = _setup(repo, tmp_path, managed, tool_use_id=None)
    _record(managed, NEW_TASK, fired_at=int(time.time()), tool_use_id=NEW_TU, task_id=NEW_TASK)
    transcript.write_text(_terminal(NEW_TASK), encoding="utf-8")
    assert _live_after(repo, transcript) == (None, None)
