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
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=_transcript(tmp_path)) == "r1"
    ended = _transcript(
        tmp_path, "<task-notification><task-id>w1</task-id><status>completed</status></task-notification>\n"
    )
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=ended) is None


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


def test_unsized_run_names_only_a_sole_live_unsized_run(repo, dirs):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1")
    assert warp_live.unsized_run(repo, "s1") == "r1"
    assert warp_live.unsized_run(repo, "s2") is None
    run_token.mint(repo, "r2", "scratch/warp/y.mjs", session_id="s1", sizing_rel=SIZING)
    assert warp_live.unsized_run(repo, "s1") is None


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


def _terminal(task, result, status="completed"):
    return (
        f"<task-notification><task-id>{task}</task-id><status>{status}</status>"
        f"<result>{result}</result></task-notification>\n"
    )


@pytest.mark.parametrize("shape", ["dependents_unstarted", "ledger-check", "plan pull"])
def test_a_halted_return_spends_the_token_and_frees_the_sizing(repo, dirs, tmp_path, shape):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path), tool_use_id="tu1")
    _launch(dirs[0], "s1", "w1")
    ended = _transcript(tmp_path, _terminal("w1", f"halted: {shape}"))
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=ended) is None
    assert not run_token.is_live(repo, "r1")
    assert list(run_token.live_records(repo)) == []
    run_token.mint(repo, "r2", SCRIPT, session_id="s1", sizing_rel=SIZING)
    assert [r["run_id"] for r in run_token.live_records(repo)] == ["r2"]


def test_a_run_whose_task_has_not_reported_terminal_stays_live_and_unspent(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path))
    _launch(dirs[0], "s1", "w1")
    other = _transcript(tmp_path, _terminal("w2", "halted: ledger-check"))
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=other) == "r1"
    assert run_token.is_live(repo, "r1")


def test_a_run_whose_commit_released_its_token_then_returned_terminal_leaves_no_token(repo, dirs, tmp_path):
    token = run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    assert run_token.redeem(repo, "r1", token, SCRIPT)
    assert run_token.release(repo, "r1", token, SCRIPT)
    _submit(repo, "r1", _transcript(tmp_path))
    _launch(dirs[0], "s1", "w1")
    ended = _transcript(tmp_path, _terminal("w1", "halted: plan pull"))
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=ended) is None
    assert list(run_token.live_records(repo)) == []
    assert not run_token.release(repo, "r1", token, SCRIPT)


def test_a_grace_expired_unlaunched_token_reads_not_live_and_is_not_spent(repo, dirs, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    real = warp_live.time.time()
    monkeypatch.setattr(warp_live.time, "time", lambda: real + warp_live.UNLAUNCHED_GRACE_SECONDS + 60)
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1") is None
    assert run_token.is_live(repo, "r1")


def test_a_refused_call_spends_the_token(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path, _result_line("tu1")))
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1") is None
    assert not run_token.is_live(repo, "r1")


def _padded(body, tmp_path, monkeypatch):
    monkeypatch.setattr(warp_live, "_TRANSCRIPT_TAIL_BYTES", 256)
    return _transcript(tmp_path, body + ('{"type":"assistant","text":"later turn"}\n' * 40))


def test_a_run_that_ended_before_the_transcript_tail_spends_its_token(repo, dirs, tmp_path, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path))
    _launch(dirs[0], "s1", "w1")
    ended = _padded(_terminal("w1", "halted: plan pull"), tmp_path, monkeypatch)
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=ended) is None
    assert not run_token.is_live(repo, "r1")


def test_a_pre_tail_run_without_a_launch_record_spends_its_token(repo, dirs, tmp_path, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    launch = json.loads(_result_line("tu1", launch=True))
    launch["toolUseResult"]["taskId"] = "w1"
    body = json.dumps(launch) + "\n" + _terminal("w1", "done")
    transcript = _padded(body, tmp_path, monkeypatch)
    _submit(repo, "r1", transcript)
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s2") is None
    assert not run_token.is_live(repo, "r1")


def test_a_pre_tail_run_with_no_terminal_notification_stays_live(repo, dirs, tmp_path, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path))
    _launch(dirs[0], "s1", "w1")
    other = _padded(_terminal("w2", "done"), tmp_path, monkeypatch)
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=other) == "r1"
    assert run_token.is_live(repo, "r1")


def test_a_terminal_notification_beyond_the_head_window_leaves_the_run_live(repo, dirs, tmp_path, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _submit(repo, "r1", _transcript(tmp_path))
    _launch(dirs[0], "s1", "w1")
    ended = _padded(_terminal("w1", "done"), tmp_path, monkeypatch)
    monkeypatch.setattr(warp_live, "_TRANSCRIPT_HEAD_BYTES", 16)
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=ended) == "r1"
    assert run_token.is_live(repo, "r1")


def test_earlier_lines_finds_lines_across_chunk_boundaries_with_bounded_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(warp_live, "_TRANSCRIPT_TAIL_BYTES", 8)
    monkeypatch.setattr(warp_live, "_SCAN_CHUNK_BYTES", 7)
    lines = [f'{{"n":{i},"pad":"{"p" * (i % 13)}"}}' for i in range(60)]
    lines[17] = '{"id":"NEEDLE-a"}'
    lines[41] = '{"id":"NEEDLE-b","pad":"' + "q" * 50 + '"}'
    path = _transcript(tmp_path, "\n".join(lines) + "\nTAILTAIL")
    reads = []
    real_open = open

    class Spy:
        def __init__(self, handle):
            self._h = handle

        def __enter__(self):
            self._h.__enter__()
            return self

        def __exit__(self, *exc):
            return self._h.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._h, name)

        def read(self, n=-1):
            reads.append(n)
            return self._h.read(n)

    monkeypatch.setattr(warp_live, "open", lambda *a, **k: Spy(real_open(*a, **k)), raising=False)
    got = warp_live._earlier_lines(path, "NEEDLE")
    assert [ln.rstrip("\r") for ln in got.split("\n")] == [lines[17], lines[41]]
    assert reads and all(0 < n <= 7 for n in reads)


def test_earlier_lines_over_a_large_head_holds_no_full_window(tmp_path, monkeypatch):
    import tracemalloc

    monkeypatch.setattr(warp_live, "_TRANSCRIPT_TAIL_BYTES", 1024)
    monkeypatch.setattr(warp_live, "_TRANSCRIPT_HEAD_BYTES", 16 * 1024 * 1024)
    filler = '{"type":"assistant","text":"' + "x" * 900 + '"}\n'
    path = _transcript(tmp_path, '{"w1":"done"}\n' + filler * (9 * 1024 * 1024 // len(filler)))
    tracemalloc.start()
    try:
        got = warp_live._earlier_lines(path, "w1")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert got == '{"w1":"done"}'
    assert peak < 4 * warp_live._SCAN_CHUNK_BYTES


def test_the_reader_names_no_system_tempdir():
    text = Path(warp_live.__file__).read_text(encoding="utf-8")
    assert "gettempdir" not in text and "sizing_mtime" not in text
