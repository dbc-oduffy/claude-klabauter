"""`measure_concurrent` reports `stall_free` false when every guard-gate slot is held, true when not."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.benchmarks import hook_ops_process_time as bench
from coordinator_core.benchmarks.preuse_dispatch_phase_cost import _build_fixture
from coordinator_core.warm import server


def _fixture(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    fx = _build_fixture(root)
    bench._write_route_table(root)
    return fx


#: The wedge cut is shortened so a held gate answers in test time; the cut's mechanism, not its value, is under test.
_TEST_BUDGET_SECS = 0.5


def test_a_wedged_guard_gate_turns_stall_free_red(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_HOOK_LANE_BUDGET_SECS", _TEST_BUDGET_SECS)
    cap = _TEST_BUDGET_SECS / 5
    out = bench.measure_concurrent(_fixture(tmp_path), hooks=6, hold_guard_gate=True, stall_cap_secs=cap)
    assert out["answered"] == 0
    assert out["stalled"] == 6
    assert out["stall_free"] is False


def test_a_wedged_guard_gate_answers_each_hook_within_the_hook_lane_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_HOOK_LANE_BUDGET_SECS", _TEST_BUDGET_SECS)
    cap = _TEST_BUDGET_SECS * 3
    out = bench.measure_concurrent(_fixture(tmp_path), hooks=6, hold_guard_gate=True, stall_cap_secs=cap)
    assert out["answered"] == 6


def test_an_unwedged_guard_gate_stays_stall_free(tmp_path):
    out = bench.measure_concurrent(_fixture(tmp_path), hooks=6, stall_cap_secs=bench.STALL_CAP_SECS)
    assert out["answered"] == 6
    assert out["stall_free"] is True


def test_a_cold_call_over_the_bar_is_surfaced_though_amortised_cpu_is_under():
    row = {"cold_ms": bench.BAR_MS + 1, "process_ms": 10.0}
    assert bench.cold_under_bar(row) is False
    assert bench.cold_under_bar({"cold_ms": bench.BAR_MS - 1}) is True
