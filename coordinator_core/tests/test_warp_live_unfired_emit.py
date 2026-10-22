"""warp_live: an emitted-but-never-fired run is not a live claim on its sizing."""

from __future__ import annotations

import json

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

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


def _unfired_run_dir(repo, run_id):
    """The R1 shape: run.json, run-baton.json and requirements.md only."""
    directory = record_dir(repo, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text("{}", encoding="utf-8")
    (directory / "run-baton.json").write_text("{}", encoding="utf-8")
    (directory / "requirements.md").write_text("# Requirements\n", encoding="utf-8")
    return directory


def test_an_unfired_emit_inside_the_grace_window_is_not_live(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    directory = _unfired_run_dir(repo, "r1")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert sorted(p.name for p in directory.iterdir()) == ["requirements.md", "run-baton.json", "run.json"]
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=str(transcript)) is None
    assert warp_live.live_run_for_sizing(repo, SIZING) is None


def test_a_launched_in_flight_run_for_the_same_sizing_is_still_live(repo, dirs, tmp_path):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    _unfired_run_dir(repo, "r1")
    (dirs[0] / "workflow-run-s1-w1.json").write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": 1.0}), encoding="utf-8"
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=str(transcript)) == "r1"


def test_an_unfired_token_does_not_mask_a_launched_run_for_the_same_sizing(repo, dirs, tmp_path):
    run_token.mint(repo, "r-unfired", "scratch/warp/old.mjs", session_id="s0", sizing_rel=SIZING)
    _unfired_run_dir(repo, "r-unfired")
    run_token.mint(repo, "r-live", SCRIPT, session_id="s1", sizing_rel=SIZING)
    (dirs[0] / "workflow-run-s1-w1.json").write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": 1.0}), encoding="utf-8"
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")
    assert (
        warp_live.live_run_for_sizing(repo, SIZING, session_id="s1", transcript_path=str(transcript)) == "r-live"
    )
