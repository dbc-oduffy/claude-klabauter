"""agent_stop_reap seeds a large unrelated store, counts only the op's own spawns, and reports the pins."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from coordinator_core.benchmarks import agent_stop_reap as bench
from coordinator_core.benchmarks import fixture_settings_home

_WIN = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: Git Bash orphan trees")


def _good():
    return {
        "records": 900, "orphan_alive": 0, "live_alive": 1, "recycled_killed": 0,
        "process_ms": 12.5, "spawns": 0, "outcomes": "killed,no-candidate,incomplete",
    }


def test_seed_unrelated_writes_old_records_and_no_markers(tmp_path):
    from coordinator_core.bash_guards import _taskstop_launch_store as store

    with fixture_settings_home(tmp_path):
        assert bench.seed_unrelated(40) == 40
        files = list(store._store_dir().glob("*.json"))
        assert len(files) == 40
        assert all(time.time() - f.stat().st_mtime > 3600 for f in files)
        assert not (store._store_dir() / "by-agent").exists()
        assert store.records_for_agent(bench._SESSION, bench._AGENT) == []


def test_spawn_counter_counts_only_inside_the_block():
    with bench.SpawnCounter() as sc:
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert sc.n == 1
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert sc.n == 1


@pytest.mark.parametrize("field,value", [
    ("orphan_alive", 1), ("live_alive", 0), ("recycled_killed", 1), ("process_ms", 500.5), ("spawns", 1),
])
def test_verdict_refuses_each_broken_pin(field, value):
    assert bench.verdict(_good())
    row = _good()
    row[field] = value
    assert not bench.verdict(row)


def test_verdict_accepts_exactly_the_bar():
    row = _good()
    row["process_ms"] = bench.BAR_MS
    assert bench.verdict(row)


def test_format_reaper_names_every_pin():
    line = bench.format_reaper(_good())
    for key in ("orphan_alive=0", "live_alive=1", "recycled_killed=0", "process_ms=12.5", "spawns=0"):
        assert key in line


@_WIN
@pytest.mark.spawns_process
def test_recorder_half_writes_the_record_without_spawning(tmp_path):
    with fixture_settings_home(tmp_path):
        bench.seed_unrelated(20)
        row = bench.measure_recorder(20, reps=2)
    assert row["wrote"] is True and row["spawns"] == 0


@_WIN
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_default_fixture_meets_the_pins_through_the_production_op(tmp_path):
    with fixture_settings_home(tmp_path):
        row = bench.measure_reaper(5)
    if "ambiguous" in str(row["outcomes"]):
        pytest.skip("peer launch in the attribution window: reaper correctly refused")
    assert bench.verdict(row), bench.format_reaper(row)
    assert row["outcomes"].split(",") == ["killed", "no-candidate", "incomplete"]


@_WIN
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_main_exit_code_follows_the_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bench, "measure_recorder", lambda n, reps=20: {
        "records": n, "process_ms": 1.0, "spawns": 0, "wrote": True, "platform": "win32"})
    rows = iter([dict(_good(), orphan_alive=1), _good()])
    monkeypatch.setattr(bench, "measure_reaper", lambda n, stagger: dict(next(rows), records=n))
    assert bench.run([7, 3], False, 0, scratch=tmp_path) == 1
    out = capsys.readouterr().out
    assert "orphan_alive=1" in out and "records=3" in out
    assert os.listdir(tmp_path) == []
