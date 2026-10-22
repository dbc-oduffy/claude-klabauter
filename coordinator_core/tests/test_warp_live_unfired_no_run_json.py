"""warp_live: a token with no run.json, submission or launch is unfired and not live."""

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


def test_a_token_with_no_run_json_no_submission_no_launch_is_not_live(repo, dirs):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    assert warp_live.live_run_for_sizing(repo, SIZING) is None
    assert warp_live.live_run_for_sizing(repo, SIZING, session_id="s1") is None
    assert any(r["run_id"] == "r1" for r in warp_live._token_records(repo))


def test_a_launched_token_with_no_run_json_is_still_live(repo, dirs):
    run_token.mint(repo, "r1", SCRIPT, session_id="s1", sizing_rel=SIZING)
    (dirs[0] / "workflow-run-s1-w1.json").write_text(
        json.dumps({"scriptPath": SCRIPT, "fired_at": 1.0}), encoding="utf-8"
    )
    assert warp_live.live_run_for_sizing(repo, SIZING) == "r1"
