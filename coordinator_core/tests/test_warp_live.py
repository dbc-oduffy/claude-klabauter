"""warp_live: one liveness reader over run tokens and launch records."""

from __future__ import annotations

import json
from pathlib import Path

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


def _launch(directory, session, task, script=SCRIPT):
    (directory / f"workflow-run-{session}-{task}.json").write_text(
        json.dumps({"scriptPath": script, "fired_at": 1.0}), encoding="utf-8"
    )


def _transcript(tmp_path, body=""):
    path = tmp_path / "t.jsonl"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_launch_record_under_the_managed_dir_is_found(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _launch(dirs[0], "s1", "w1")
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=_transcript(tmp_path)) == "r1"
    ended = _transcript(
        tmp_path, "<task-notification><task-id>w1</task-id><status>completed</status></task-notification>\n"
    )
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=ended) is None


def test_launch_record_under_the_legacy_dir_is_found(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _launch(dirs[1], "s1", "w1")
    ended = _transcript(
        tmp_path, "<task-notification><task-id>w1</task-id><status>completed</status></task-notification>\n"
    )
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=ended) is None
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=_transcript(tmp_path)) == "r1"


def test_an_explicit_tmpdir_reads_that_directory_only(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    other = tmp_path / "other"
    other.mkdir()
    _launch(dirs[0], "s1", "w1")
    ended = _transcript(
        tmp_path, "<task-notification><task-id>w1</task-id><status>completed</status></task-notification>\n"
    )
    # Launch record is in the managed dir, not `other`: unlaunched and inside grace reads live.
    assert warp_live.live_run_for_sizing(
        repo, SIZING, session_id="s1", transcript_path=ended, tmpdir=str(other)
    ) == "r1"


def test_a_raw_ask_token_owns_no_sizing(repo, dirs):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1")
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1") is None


def test_an_absolute_or_dotted_sizing_path_matches(repo, dirs):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    assert warp_live.live_run_for_sizing(repo, str(repo / SIZING)) == "r1"
    assert warp_live.live_run_for_sizing(repo, "./" + SIZING) == "r1"
    assert warp_live.live_run_for_sizing(repo, "state/sizings/other.yaml") is None


def test_live_run_for_session_answers_for_sizing_bound_and_raw_ask(repo, dirs):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    run_token.mint(repo, "r2", "scratch/warp/y.mjs", session_id="s2")
    assert warp_live.live_run_for_session(repo, "s1") == "r1"
    assert warp_live.live_run_for_session(repo, "s2") == "r2"
    assert warp_live.live_run_for_session(repo, "s3") is None
    assert warp_live.live_run_for_session(repo, None) is None


def test_a_consumed_token_is_not_live(repo, dirs):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    run_token.consume(repo, "r1")
    assert warp_live.live_run_for_session(repo, "s1") is None
    assert warp_live.live_run_for_sizing(repo, SIZING) is None


def _submit(repo, run_id, transcript, tool_use_id="tu1", session="s1"):
    warp_live.record_submission(
        repo, run_id, session_id=session, transcript_path=transcript, script_path=SCRIPT,
        sizing_rel=SIZING, tool_use_id=tool_use_id,
    )


def _result_line(tool_use_id, launch=False):
    obj = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id}]}}
    if launch:
        obj["toolUseResult"] = {"status": "async_launched", "taskType": "local_workflow"}
    return json.dumps(obj) + "\n"


def test_unlaunched_for_own_session_once_the_call_was_refused(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    transcript = _transcript(tmp_path, _result_line("tu1"))
    _submit(repo, "r1", transcript)
    assert warp_live.unlaunched_run_for_sizing(repo, SIZING, "s1") == "r1"


def test_not_unlaunched_for_another_session(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path, _result_line("tu1")))
    assert warp_live.unlaunched_run_for_sizing(repo, SIZING, "s2") is None


def test_not_unlaunched_once_a_launch_record_exists(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path, _result_line("tu1")))
    _launch(dirs[0], "s1", "w1")
    assert warp_live.unlaunched_run_for_sizing(repo, SIZING, "s1") is None


def test_not_unlaunched_while_the_call_has_no_result_or_launched(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    in_flight = _transcript(tmp_path, '{"type":"assistant","id":"tu1","name":"tool_use"}\n')
    _submit(repo, "r1", in_flight)
    assert warp_live.unlaunched_run_for_sizing(repo, SIZING, "s1") is None
    _submit(repo, "r1", _transcript(tmp_path, _result_line("tu1", launch=True)))
    assert warp_live.unlaunched_run_for_sizing(repo, SIZING, "s1") is None


def test_not_unlaunched_when_the_submission_stored_no_tool_use_id(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path, _result_line("tu1")), tool_use_id=None)
    assert warp_live.unlaunched_run_for_sizing(repo, SIZING, "s1") is None


def test_a_refused_call_token_is_not_live_for_sizing_or_session(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path, _result_line("tu1")))
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1") is None
    assert warp_live.live_run_for_session(repo, "s1") is None


def test_another_sessions_refused_token_is_not_live(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path, _result_line("tu1")))
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s2") is None
    assert warp_live.live_run_for_sizing(repo, SIZING) is None


def test_an_in_flight_call_token_stays_live(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path, '{"type":"assistant","id":"tu1","name":"tool_use"}\n'))
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s2") == "r1"
    assert warp_live.live_run_for_session(repo, "s1") == "r1"


def test_a_launched_run_stays_live_despite_a_result_line(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path, _result_line("tu1", launch=True)))
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s2") == "r1"
    _submit(repo, "r1", _transcript(tmp_path, _result_line("tu1")))
    _launch(dirs[0], "s1", "w1")
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s2") == "r1"


def test_the_reader_names_no_system_tempdir():
    text = Path(warp_live.__file__).read_text(encoding="utf-8")
    assert "gettempdir" not in text and "sizing_mtime" not in text
