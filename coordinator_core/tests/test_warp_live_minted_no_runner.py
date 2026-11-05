"""warp_live: a minted run with no runner past the short grace is unproven, not working."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core import launch_records, warp_live
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

SIZING = "state/sizings/mine.yaml"
SCRIPT = "scratch/warp/x.mjs"
GRACE = warp_live.MINTED_RUNNER_GRACE_SECONDS


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


def _mint(repo, run_id="r1"):
    run_token.mint(repo, run_id, SCRIPT, session_id="s1", sizing_rel=SIZING)
    return next(r for r in run_token.live_records(repo) if r["run_id"] == run_id)


def _stage(repo, run_id, stage):
    directory = record_dir(repo, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")


def _submit(repo, run_id, transcript=None, tool_use_id=None):
    warp_live.record_submission(
        repo, run_id, session_id="s1", transcript_path=str(transcript) if transcript else None,
        script_path=SCRIPT, sizing_rel=SIZING, tool_use_id=tool_use_id,
    )


def _late(record):
    return record["minted_at"] + GRACE + 5


def test_a_minted_run_with_no_runner_past_the_grace_is_unproven(repo, dirs):
    record = _mint(repo)
    _submit(repo, "r1")
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is True


def test_an_absent_stage_reads_as_minted(repo, dirs):
    record = _mint(repo)
    assert not (record_dir(repo, "r1") / "run-stage.json").exists()
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is True


def test_inside_the_grace_the_run_is_not_unproven(repo, dirs):
    record = _mint(repo)
    assert warp_live.runner_unproven(repo, "r1", now=record["minted_at"] + GRACE - 5) is False


def test_a_staged_run_is_never_unproven(repo, dirs):
    record = _mint(repo)
    _stage(repo, "r1", "staged")
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is False


def test_a_malformed_stage_is_unknown_and_not_unproven(repo, dirs):
    record = _mint(repo)
    directory = record_dir(repo, "r1")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run-stage.json").write_text("{not json", encoding="utf-8")
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is False


def test_a_launch_record_proves_a_runner(repo, dirs):
    record = _mint(repo)
    _submit(repo, "r1")
    launch_records_path = launch_records.record_path(str(dirs[0]), "s1", "w1")
    Path(launch_records_path).write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": record["minted_at"] + 1}), encoding="utf-8"
    )
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is False


def test_a_launch_result_in_the_transcript_proves_a_runner(repo, dirs, tmp_path):
    record = _mint(repo)
    transcript = tmp_path / "t.jsonl"
    line = {
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1"}]},
        "toolUseResult": {"status": "async_launched", "taskId": "w9"},
    }
    transcript.write_text(json.dumps(line) + "\n", encoding="utf-8")
    _submit(repo, "r1", transcript, "toolu_1")
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is False


def test_an_unreadable_transcript_is_unknown_and_not_unproven(repo, dirs, tmp_path):
    record = _mint(repo)
    _submit(repo, "r1", tmp_path / "missing.jsonl", "toolu_1")
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is False


def _forbid_transcript_reads(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("transcript read")

    for name in ("_tail", "_earlier_lines", "_result_names_run"):
        monkeypatch.setattr(warp_live, name, boom)


def test_runner_unproven_reads_no_transcript(repo, dirs, tmp_path, monkeypatch):
    """Records decide: no submitted call or transcript and no launch is True; a submission is False."""
    record = _mint(repo)
    _submit(repo, "r1")
    _forbid_transcript_reads(monkeypatch)
    opened = []
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is True
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: opened.append(str(p)) or real_open(p, *a, **k))
    _submit(repo, "r1", transcript, "toolu_1")
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is False
    assert str(transcript) not in opened


def test_a_submitted_call_with_no_launch_record_is_not_unproven(repo, dirs):
    """Unknown is not runner-less: the launch result that once proved it lives only in the transcript."""
    record = _mint(repo)
    _submit(repo, "r1", None, "toolu_1")
    assert warp_live.runner_unproven(repo, "r1", now=_late(record)) is False


def _run_ended_records_only(repo, record, **kw):
    return warp_live._run_ended(
        "s1", SCRIPT, kw.pop("transcript", None), record["minted_at"], [], None,
        run_id="r1", repo_root=repo, records_only=True, **kw,
    )


def test_run_ended_records_only_reads_no_transcript(repo, dirs, tmp_path, monkeypatch):
    """Records-only: no launch inside the minted grace is live; past it the timer fires; no read."""
    record = _mint(repo)
    _forbid_transcript_reads(monkeypatch)
    transcript = str(tmp_path / "t.jsonl")
    monkeypatch.setattr(warp_live.time, "time", lambda: record["minted_at"] + GRACE - 5)
    assert _run_ended_records_only(repo, record, transcript=transcript) is None
    monkeypatch.setattr(warp_live.time, "time", lambda: _late(record))
    assert _run_ended_records_only(repo, record, transcript=transcript) == warp_live._TIMER


def test_run_ended_records_only_with_a_launch_reads_live(repo, dirs, monkeypatch):
    """A launched run's terminal notification is only in the transcript, so records-only reads it live."""
    record = _mint(repo)
    Path(launch_records.record_path(str(dirs[0]), "s1", "w1")).write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": record["minted_at"] + 1}), encoding="utf-8"
    )
    _forbid_transcript_reads(monkeypatch)
    monkeypatch.setattr(warp_live.time, "time", lambda: _late(record) + 10 * GRACE)
    ended = warp_live._run_ended(
        "s1", SCRIPT, None, record["minted_at"], [str(dirs[0])], None, run_id="r1", repo_root=repo, records_only=True
    )
    assert ended is None


def test_record_live_records_only_does_not_spend_the_token_on_owner_exit(repo, dirs, monkeypatch):
    record = _mint(repo)
    monkeypatch.setattr(warp_live, "_owner_exited", lambda *_a: True)
    assert warp_live._record_live(repo, record, None, None, [], records_only=True) is False
    assert run_token.is_live(repo, "r1")
    assert warp_live._record_live(repo, record, None, None, []) is False
    assert not run_token.is_live(repo, "r1")


def test_no_token_record_is_not_unproven(repo, dirs):
    assert warp_live.runner_unproven(repo, "ghost", now=time.time() + 10 * GRACE) is False


def test_live_run_for_sizing_retires_a_runnerless_minted_run_past_the_grace(repo, dirs, monkeypatch):
    record = _mint(repo)
    _submit(repo, "r1")
    assert warp_live.live_run_for_sizing(repo, SIZING) == "r1"
    monkeypatch.setattr(warp_live.time, "time", lambda: _late(record))
    assert warp_live.live_run_for_sizing(repo, SIZING) is None
    assert run_token.is_live(repo, "r1")


def test_a_staged_run_keeps_the_long_grace(repo, dirs, monkeypatch):
    record = _mint(repo)
    _stage(repo, "r1", "staged")
    _submit(repo, "r1")
    monkeypatch.setattr(warp_live.time, "time", lambda: _late(record))
    assert warp_live.live_run_for_sizing(repo, SIZING) == "r1"
    monkeypatch.setattr(warp_live.time, "time", lambda: record["minted_at"] + warp_live.UNLAUNCHED_GRACE_SECONDS + 5)
    assert warp_live.live_run_for_sizing(repo, SIZING) is None
