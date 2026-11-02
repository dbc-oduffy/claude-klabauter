"""publish.py main hands the warm pair over with live runs present.

Falsifiers: (a) a live run reaches the stop and the land instead of refusing; (b) the live-run list is
read with the publish marker not held, or not immediately before the land-time stop.

Run: python -m pytest coordinator/bin/tests/test_publish_respawn_race.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_publish_quiesces_warm_pair as _q  # noqa: E402

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_RUN = ("example-game-repo", "ask-be779d", "implement", "2026-10-31T22:46:00Z")


def test_live_run_hands_over_with_stop_called_and_land_reached(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs

    env = _q._setup(tmp_path, monkeypatch, True)
    env.live_runs[:] = [live_runs.LiveRun(*_RUN)]
    rc, err = _q._run(env)
    assert rc == 0, err
    assert len(env.names("stop")) == 2 and env.names("land_diff") == [("land_diff", True)]


def test_live_runs_are_read_with_the_marker_held_immediately_before_the_land_stop(tmp_path, monkeypatch):
    env = _q._setup(tmp_path, monkeypatch, True)
    rc, err = _q._run(env)
    assert rc == 0, err
    reads = env.names("find_live_runs")
    assert reads and all(held is True for _, held in reads), reads
    assert env.events[-3:] == [("find_live_runs", True), ("stop", True), ("land_diff", True)], env.events
    assert max(i for i, _ in env.gate_events()) < len(env.events) - 3
