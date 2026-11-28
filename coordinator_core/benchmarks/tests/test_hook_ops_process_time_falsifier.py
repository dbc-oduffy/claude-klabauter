"""`measure_concurrent` reports `stall_free` false when every guard-gate slot is held, true when not."""

from __future__ import annotations

import json
import os
import time
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


def test_the_io_counter_counts_open_and_listdir_inside_the_block_only(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    with bench._IOCounter() as io:
        target.read_text(encoding="utf-8")
        os.listdir(tmp_path)
    assert io.count >= 2
    before = io.count
    target.read_text(encoding="utf-8")
    assert io.count == before


def test_a_wedged_op_reads_over_the_dispatch_bar_and_its_io_is_counted(tmp_path, monkeypatch):
    from coordinator_core.warm import entry_seam

    probe = tmp_path / "probe.txt"
    probe.write_text("x", encoding="utf-8")

    def wedged(op, payload):
        end = time.process_time() + (bench.DISPATCH_BAR_MS + 20.0) / 1000.0
        while time.process_time() < end:
            pass
        for _ in range(40):
            probe.read_text(encoding="utf-8")
        return {}

    monkeypatch.setattr(entry_seam, "reentrant_dispatch", wedged)
    row = bench.measure_op(bench.OP_PREUSE_BASH, {}, 1)
    assert bench.under_dispatch_bar(row) is False
    assert row["io_ops_per_call"] >= 40


def test_a_cheap_op_reads_under_the_dispatch_bar():
    assert bench.under_dispatch_bar({"server_cpu_ms": bench.DISPATCH_BAR_MS - 1}) is True
    assert bench.under_dispatch_bar({"server_cpu_ms": bench.DISPATCH_BAR_MS}) is False


def test_the_baseline_keeps_one_row_per_label_and_phase(tmp_path):
    path = tmp_path / "b.json"
    for phase, head in (("quiet", "a"), ("bulk-delete", "b"), ("quiet", "c")):
        bench.write_row({"head": head, "phase": phase}, "base", path)
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    assert sorted((r["phase"], r["head"]) for r in rows) == [("bulk-delete", "b"), ("quiet", "c")]


def test_a_row_without_a_phase_is_replaced_by_the_quiet_row(tmp_path):
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"schema": 1, "rows": [{"label": "base", "head": "old"}]}), encoding="utf-8")
    bench.write_row({"head": "new", "phase": "quiet"}, "base", path)
    assert [r["head"] for r in json.loads(path.read_text(encoding="utf-8"))["rows"]] == ["new"]


def test_a_cold_call_over_the_bar_is_surfaced_though_amortised_cpu_is_under():
    row = {"cold_ms": bench.BAR_MS + 1, "process_ms": 10.0}
    assert bench.cold_under_bar(row) is False
    assert bench.cold_under_bar({"cold_ms": bench.BAR_MS - 1}) is True
