"""warp_live: a run whose submitting session has no live registry row is ended; its token is consumed."""

from __future__ import annotations

import json
import os

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
def sessions(tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    (managed / "workflow-run-s1-w1.json").write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": 1.0}), encoding="utf-8"
    )
    config = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config / "sessions"


@pytest.fixture
def fired(repo, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    warp_live.record_submission(
        repo, "r1", session_id="s1", transcript_path=str(transcript), script_path=SCRIPT,
        sizing_rel=SIZING, tool_use_id="tu1",
    )
    return str(transcript)


def _live(repo, transcript):
    return warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=transcript)


def _row(sessions, session_id, pid):
    (sessions / f"{pid}.json").write_text(
        json.dumps({"sessionId": session_id, "pid": pid}), encoding="utf-8"
    )


def test_no_row_in_an_existing_registry_past_the_grace_window_ends_the_run_and_consumes_the_token(
    repo, sessions, fired, monkeypatch
):
    sessions.mkdir(parents=True)
    _row(sessions, "other", os.getpid())
    real = warp_live.time.time()
    monkeypatch.setattr(warp_live.time, "time", lambda: real + warp_live.OWNER_ROW_GRACE_SECONDS + 5)
    assert _live(repo, fired) is None
    assert not run_token.is_live(repo, "r1")


def test_row_with_a_dead_pid_ends_the_run(repo, sessions, fired):
    sessions.mkdir(parents=True)
    _row(sessions, "s1", 2**22 + 12345)
    assert _live(repo, fired) is None


def test_row_with_a_live_pid_keeps_the_run_live(repo, sessions, fired):
    sessions.mkdir(parents=True)
    _row(sessions, "s1", os.getpid())
    assert _live(repo, fired) == "r1"
    assert run_token.is_live(repo, "r1")


def test_absent_registry_dir_keeps_the_run_live(repo, sessions, fired):
    assert not sessions.exists()
    assert _live(repo, fired) == "r1"
    assert run_token.is_live(repo, "r1")
