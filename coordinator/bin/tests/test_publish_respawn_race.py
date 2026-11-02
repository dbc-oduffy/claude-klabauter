"""publish.py main hands the warm pair over with live runs present.

Falsifiers: (a) a live run reaches the stop and the round instead of refusing; (b) the live-run list is
read with the publish marker already held.

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


def test_live_run_hands_over_with_stop_called_and_round_reached(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs

    publish_mod, dest, calls, bc, ps = _q._setup(tmp_path, monkeypatch, True)
    _q._refresh_ok(monkeypatch)
    monkeypatch.setattr(live_runs, "find_live_runs", lambda *a, **k: [live_runs.LiveRun(*_RUN)])
    stops = []
    monkeypatch.setattr(
        ps, "stop_warm_pair",
        lambda root, **k: stops.append(root) or ps.PairStopResult(stopped=(), survivors=()),
    )
    rc, err = _q._run(publish_mod)
    assert rc == "round-reached", err
    assert len(stops) == 1 and calls == [("round", True)]


def test_live_runs_are_read_after_the_publish_marker_is_held(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs

    publish_mod, dest, calls, bc, ps = _q._setup(tmp_path, monkeypatch, True)
    _q._refresh_ok(monkeypatch)
    held_at_read = []

    def find(*a, **k):
        held_at_read.append(bc.publish_marker_held(bc.svc_dir(dest)))
        return []

    monkeypatch.setattr(live_runs, "find_live_runs", find)
    monkeypatch.setattr(
        ps, "stop_warm_pair", lambda root, **k: ps.PairStopResult(stopped=(), survivors=())
    )
    _q._run(publish_mod)
    assert held_at_read and held_at_read[-1] is True, held_at_read

