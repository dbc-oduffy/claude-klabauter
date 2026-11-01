"""publish.py main never cuts runs and then aborts on warm-pair survivors.

Falsifiers: (a) a live run the override does not name refuses before any stop; (b) the live-run list is
read with the publish marker already held.

Run: python -m pytest coordinator/bin/tests/test_publish_respawn_race.py -q
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_publish_quiesces_warm_pair as _q  # noqa: E402

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_RUN = ("example-game-repo", "ask-be779d", "implement", "2026-10-31T22:46:00Z")


def test_live_run_unnamed_by_override_refuses_with_stop_never_called(tmp_path, monkeypatch):
    from coordinator_core.warm import live_runs

    publish_mod, dest, calls, bc, ps = _q._setup(tmp_path, monkeypatch, True)
    _q._refresh_ok(monkeypatch)
    monkeypatch.setattr(live_runs, "find_live_runs", lambda *a, **k: [live_runs.LiveRun(*_RUN)])
    stops = []
    monkeypatch.setattr(
        ps, "stop_warm_pair",
        lambda root, **k: stops.append(root) or ps.PairStopResult(stopped=(), survivors=()),
    )
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            rc = publish_mod.main(["row-a", "--allow-mid-stage", "rollback of a bad release"])
        except _q._Stop:
            rc = "round-reached"
    assert rc == 1, err.getvalue()
    assert stops == [] and calls == []


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

