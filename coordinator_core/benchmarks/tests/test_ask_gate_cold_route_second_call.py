"""The cold-route ask_gate benchmark bills two back-to-back calls and a peer-lock variant.

Behaviour pinned: the fixture holds the requested run-dir count plus the incident run; both calls ride
the cold route in their own child process, bind the run to the sizing and answer with no error code
(never -32004); every promised phase carries process and wall figures; the second call names its
dominant phase; a peer holding the sizing's lock shows as lock-wait wall time in the third call.
"""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import ask_gate_cold_route_second_call as bench

pytestmark = pytest.mark.cadence

DIRS = 40
HOLD_SECS = 0.8


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    import os

    home = tmp_path_factory.mktemp("settings-home")
    os.environ["COORDINATOR_SETTINGS_HOME"] = str(home)
    try:
        return bench.measure(dirs=DIRS, hold_secs=HOLD_SECS)
    finally:
        os.environ.pop("COORDINATOR_SETTINGS_HOME", None)


def test_fixture_holds_the_requested_dir_count_plus_the_incident_run(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    root = tmp_path / "repo"
    root.mkdir()
    sizing_rel = bench.build_fixture(root, DIRS)
    warp = root / "state" / "warp"
    assert sum(1 for p in warp.iterdir() if p.is_dir()) == DIRS + 1
    assert (warp / bench.RUN_ID / "run.json").is_file()
    assert (root / sizing_rel).is_file()


def test_both_calls_are_cold_route_bound_and_never_hang_past_the_ceiling(report):
    for key in ("first_call", "second_call"):
        call = report[key]
        assert call["error_code"] is None, (key, call["error_message"])
        assert call["halt"] is None, (key, call)
        assert call["bound_sizing_path"] == f"state/sizings/{bench.SIZING_STEM}.yaml", (key, call)
        assert call["procs"] >= 1 and isinstance(call["spawns"], int)


def test_a_peer_held_lock_refuses_fast_rather_than_hanging_past_the_ceiling(report):
    held = report["second_call_peer_lock"]
    assert held["error_code"] != bench.HUNG_PAST_CEILING_CODE
    assert held["halt"] == "refusal" and held["bound_sizing_path"] is None, held
    assert held["wall_ms"] < 5000.0, held["wall_ms"]


def test_every_promised_phase_is_billed_and_the_dominant_one_is_named(report):
    for key in ("first_call", "second_call", "second_call_peer_lock"):
        call = report[key]
        assert set(call["phases"]) == set(bench.PHASES)
        assert all(p["process_ms"] >= 0.0 for p in call["phases"].values())
        assert call["dominant_phase"] in bench.PHASES
    assert report["second_call_peer_lock"]["phases"]["lock_waits"]["calls"] >= 1
    assert report["second_call_dominant_phase"] == report["second_call"]["dominant_phase"]
    assert report["interpreter_floor_ms"] >= 0.0 and report["bar_ms"] == bench.BAR_MS


def test_a_peer_holding_the_lock_shows_as_lock_wait_wall_time_not_cpu(report):
    held = report["second_call_peer_lock"]
    free = report["second_call"]
    assert held["peer_hold_secs"] == HOLD_SECS
    from coordinator_core.ops.dispatch_emit.ask_gate import GATE_LOCK_WAIT_SECS

    wait = held["phases"]["lock_waits"]["wall_ms"]
    assert GATE_LOCK_WAIT_SECS * 1000.0 * 0.8 <= wait < HOLD_SECS * 1000.0, held["phases"]["lock_waits"]
    assert held["phases"]["lock_waits"]["wall_ms"] > free["phases"]["lock_waits"]["wall_ms"]
