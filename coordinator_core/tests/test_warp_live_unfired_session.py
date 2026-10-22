"""warp_live: an unfired token does not read as its session's live run."""

from __future__ import annotations

import json

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import run_token

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


def test_an_unfired_token_is_not_the_sessions_live_run(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="sX", sizing_rel=None)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert warp_live.live_run_for_session(repo, "sX", transcript_path=str(transcript)) is None


def test_a_launched_in_flight_token_is_still_the_sessions_live_run(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="sX", sizing_rel=None)
    (dirs[0] / "workflow-run-sX-w1.json").write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": 1.0}), encoding="utf-8"
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert warp_live.live_run_for_session(repo, "sX", transcript_path=str(transcript)) == "r1"
