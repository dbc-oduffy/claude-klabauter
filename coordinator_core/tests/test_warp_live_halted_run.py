"""warp_live: a run holding a halted or landed marker is never live, even with an unconsumed token."""

from __future__ import annotations

import json

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import halted_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

SIZING = "state/sizings/mine.yaml"
SCRIPT = "scratch/warp/run.workflow.mjs"
RUN = "warp-mine-20261021T191648-bb4d27"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    run_token.mint(root, RUN, SCRIPT, session_id="s1", sizing_rel=SIZING)
    warp_live.record_submission(
        root, RUN, session_id="s1", transcript_path=None, script_path=SCRIPT, sizing_rel=SIZING
    )
    return root


def _live(root):
    return (
        warp_live.live_run_for_sizing(root, SIZING, session_id="s1"),
        warp_live.live_run_for_session(root, "s1"),
    )


def _mark(root, status):
    d = root / "state" / "warp" / RUN
    d.mkdir(parents=True, exist_ok=True)
    (d / halted_run.HALTED_FILE).write_text(json.dumps({"status": status}), encoding="utf-8")


def test_unmarked_run_with_live_token_reads_live(repo):
    assert _live(repo) == (RUN, RUN)


@pytest.mark.parametrize("status", ["halted", "landed"])
def test_halted_or_landed_marker_overrides_a_stale_live_token(repo, status):
    _mark(repo, status)
    assert run_token.is_live(repo, RUN)
    assert _live(repo) == (None, None)


def test_resumed_marker_still_reads_live(repo):
    _mark(repo, "resumed")
    assert _live(repo) == (RUN, RUN)


def test_halted_agrees_with_revise_run_live(repo):
    _mark(repo, "halted")
    d = record_dir(repo, RUN)
    halted_run.mark_revised(d, "2026-10-21T19:30:00Z")
    revise_live = run_token.is_live(repo, RUN) and halted_run.revised_halt(d) is None
    assert revise_live is False
    assert _live(repo) == (None, None)
