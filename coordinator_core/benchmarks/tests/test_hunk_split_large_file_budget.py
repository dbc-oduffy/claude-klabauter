"""Standing gate: the hunk split and `dispatch.land_run` on ~70k-line repetitive maps stay under the
DR-344 bar (500ms process time per axis, never the 2000ms suspension bar), and the benchmark never
writes to the real machine-local registry.

SHAPE. `hunk_split_large_file_budget.run` at its default arguments measures every axis in its own
child process; a quadratic pass is killed at the cap and reported `timed_out`, which fails the bar.
The token axis is asserted separately: its figure is the second land_run after an abandoned first,
and a `run-token-in-flight` refusal fails it whatever its time.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import hunk_split_large_file_budget as bench

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TIMING_AXES = tuple(a for a in bench.AXES if a != bench.AXIS_TOKEN)


@pytest.fixture(scope="module")
def rows() -> dict:
    measured = bench.run(bench.DEFAULT_LINES, bench.DEFAULT_CAP_SECS)
    return {r["axis"]: r for r in measured}


@pytest.mark.parametrize("axis", TIMING_AXES)
def test_axis_is_under_the_brightline_bar(rows, axis):
    row = rows[axis]
    assert not row.get("error"), row
    assert not row.get("timed_out"), row
    assert row["process_time_ms"] < bench.BAR_MS, row
    assert bench.BAR_MS == 500.0


def test_second_land_after_abandoned_is_not_refused_in_flight(rows):
    row = rows[bench.AXIS_TOKEN]
    assert not row.get("error"), row
    assert row.get("refusal") != bench.IN_FLIGHT, row
    assert row["process_time_ms"] < bench.BAR_MS, row


def test_axis_past_its_cap_is_a_timed_out_row_not_a_blocked_run(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    row = bench.measure_axis(bench.AXIS_LAND, tmp_path, bench.DEFAULT_LINES, 0.01)
    assert row["timed_out"] is True, row
    assert row["process_time_is_lower_bound"] is True, row
    assert bench.over_bar(row) is True


def test_over_bar_flags_the_refusal_and_the_slow_row():
    assert bench.over_bar({"axis": bench.AXIS_LAND, "process_time_ms": 499.0}) is False
    assert bench.over_bar({"axis": bench.AXIS_LAND, "process_time_ms": 500.0}) is True
    assert bench.over_bar({"axis": bench.AXIS_LAND, "timed_out": True}) is True
    assert bench.over_bar({"axis": bench.AXIS_TOKEN, "process_time_ms": 1.0, "refusal": bench.IN_FLIGHT}) is True
    assert bench.over_bar({"axis": bench.AXIS_TOKEN, "process_time_ms": 1.0, "refusal": None}) is False


def _real_engine_records() -> Path:
    from coordinator_core import _settings_home

    return _settings_home.machine_local_dir() / "engine-records"


def _fixture_files_newer_than(root: Path, start: float) -> list:
    if not root.is_dir():
        return []
    hits = []
    for path in root.rglob(f"*{bench.RUN}*"):
        try:
            if path.stat().st_mtime >= start:
                hits.append(str(path))
        except OSError:
            continue
    return hits


def test_run_writes_nothing_to_the_real_registry(tmp_path, monkeypatch):
    real = _real_engine_records()
    start = time.time()
    time.sleep(0.05)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "s"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "c"))
    rows = bench.run(2000, 120.0, axes=(bench.AXIS_LAND,))
    assert rows and not rows[0].get("error"), rows
    assert os.environ["COORDINATOR_SETTINGS_HOME"] == str(tmp_path / "s")
    assert _fixture_files_newer_than(real, start) == []
