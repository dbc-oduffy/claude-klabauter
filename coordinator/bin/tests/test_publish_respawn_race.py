"""publish.py main hands the warm pair over with live runs present.

Falsifiers: (a) a live run reaches the land with no stop across it (make-before-break: the old pair is
left answering until the successor starts); (b) the last live-run read is not under the held publish
marker, or not immediately before the land.

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


def test_live_run_hands_over_with_no_stop_across_the_land(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs

    env = _q._setup(tmp_path, monkeypatch, True)
    env.live_runs[:] = [live_runs.LiveRun(*_RUN)]
    rc, err = _q._run(env)
    assert rc == 0, err
    assert env.names("stop") == [] and env.names("land_diff") == [("land_diff", True)]


def test_live_runs_are_read_with_the_marker_held_immediately_before_the_land(tmp_path, monkeypatch):
    env = _q._setup(tmp_path, monkeypatch, True)
    rc, err = _q._run(env)
    assert rc == 0, err
    reads = env.names("find_live_runs")
    assert reads and reads[-1] == ("find_live_runs", True), reads
    land = env.events.index(("land_diff", True))
    assert env.events[land - 1] == ("find_live_runs", True), env.events
    assert max(i for i, _ in env.gate_events()) < land - 1
