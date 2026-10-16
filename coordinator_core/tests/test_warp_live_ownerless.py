"""warp_live: an ownerless run token ages out on the unlaunched grace and is never spent."""

from __future__ import annotations

import json

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


def _past_grace(monkeypatch):
    real = warp_live.time.time()
    monkeypatch.setattr(warp_live.time, "time", lambda: real + warp_live.UNLAUNCHED_GRACE_SECONDS + 60)


def test_ownerless_token_ages_out_without_a_submission(repo, dirs, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id=None, sizing_rel=SIZING)
    assert warp_live.live_run_for_sizing(repo, SIZING) == "r1"
    _past_grace(monkeypatch)
    assert warp_live.live_run_for_sizing(repo, SIZING) is None
    assert run_token.is_live(repo, "r1")


def test_ownerless_token_with_an_ownerless_submission_ages_out(repo, dirs, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id=None, sizing_rel=SIZING)
    warp_live.record_submission(
        repo, "r1", session_id=None, transcript_path=None, script_path=SCRIPT, sizing_rel=SIZING
    )
    assert warp_live.live_run_for_sizing(repo, SIZING) == "r1"
    _past_grace(monkeypatch)
    assert warp_live.live_run_for_sizing(repo, SIZING) is None
    assert run_token.is_live(repo, "r1")


def test_an_asker_never_adopts_an_ownerless_run(repo, dirs, tmp_path, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id=None, sizing_rel=SIZING)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    _past_grace(monkeypatch)
    assert warp_live.live_run_for_sizing(
        repo, SIZING, session_id="s9", transcript_path=str(transcript)
    ) is None


def test_an_owned_launched_run_without_terminal_stays_live(repo, dirs, tmp_path, monkeypatch):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    (dirs[0] / "workflow-run-s1-w1.json").write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": 1.0}), encoding="utf-8"
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    _past_grace(monkeypatch)
    assert warp_live.live_run_for_sizing(
        repo, SIZING, session_id="s1", transcript_path=str(transcript)
    ) == "r1"
    assert run_token.is_live(repo, "r1")


def test_run_ended_unit_contract(monkeypatch):
    minted = warp_live.time.time()
    assert warp_live._run_ended(None, SCRIPT, None, minted, [], None) is None
    _past_grace(monkeypatch)
    assert warp_live._run_ended(None, SCRIPT, None, minted, [], None) == warp_live._TIMER
