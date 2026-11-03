"""Census join, per-leg attribution and the unjoined bucket, over a synthetic sink and record dir."""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core.benchmarks import land_run_over_bar_census as census


def _process(corr, t_start, ms, **extra):
    return {"op": census.OP, "kind": "process_time", "corr_id": corr, "t_start": t_start,
            "process_ms": ms, **extra}


def _complete(corr, t_start, elapsed_ms):
    return {"op": census.OP, "kind": "complete", "corr_id": corr, "t_start": t_start,
            "elapsed_ms": elapsed_ms}


def _sink(tmp_path: Path, rows: list) -> Path:
    path = tmp_path / "op-latency.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _record(tmp_path: Path, run_id: str, mtime: float, legs: dict) -> str:
    target = tmp_path / "state" / "warp" / run_id / "land-legs.json"
    target.parent.mkdir(parents=True)
    body = {"outcome": "landed", "leg": None, "bound_by": None,
            "legs": [{"leg": k, "elapsed_secs": 1.0, "process_time_ms": v, "spawns": 0}
                     for k, v in legs.items()]}
    target.write_text(json.dumps(body), encoding="utf-8")
    os.utime(target, (mtime, mtime))
    return str(target)


def test_over_bar_join_and_per_leg_attribution(tmp_path):
    t0 = 1_000_000.0
    rows = [
        _process("a", t0, 900.0), _complete("a", t0, 2000.0),
        _process("b", t0 + 100, 1500.0), _complete("b", t0 + 100, 3000.0),
        _process("fast", t0 + 200, 100.0),
    ]
    _record(tmp_path, "run-a", t0 + 2.0, {"land_run.script_read": 50.0, "land_run.integrity": 700.0})
    _record(tmp_path, "run-b", t0 + 103.0, {"land_run.script_read": 60.0, "land_run.integrity": 1200.0})
    process_rows, elapsed = census.read_rows([_sink(tmp_path, rows)], t0 - 1, t0 + 1000)
    over = [r for r in process_rows if r["process_ms"] > census.BAR_MS]
    joined, unjoined = census.join_rows(over, elapsed, census.record_files(tmp_path))
    assert len(process_rows) == 3 and len(over) == 2 and len(joined) == 2 and not unjoined
    assert {r["corr_id"]: rec["legs"][1]["process_time_ms"] for r, rec in joined} == {"a": 700.0, "b": 1200.0}


def test_unjoined_rows_are_counted_not_dropped(tmp_path):
    t0 = 2_000_000.0
    rows = [
        _process("nocomplete", t0, 800.0),
        _process("nofile", t0 + 50, 800.0), _complete("nofile", t0 + 50, 1000.0),
    ]
    process_rows, elapsed = census.read_rows([_sink(tmp_path, rows)], t0 - 1, t0 + 1000)
    joined, unjoined = census.join_rows(process_rows, elapsed, [])
    assert joined == [] and len(unjoined) == 2


def test_a_file_is_claimed_by_one_row_only(tmp_path):
    t0 = 3_000_000.0
    rows = [_process("x", t0, 800.0), _complete("x", t0, 1000.0),
            _process("y", t0 + 0.1, 800.0), _complete("y", t0 + 0.1, 1000.0)]
    _record(tmp_path, "only", t0 + 1.0, {"land_run.commit": 700.0})
    process_rows, elapsed = census.read_rows([_sink(tmp_path, rows)], t0 - 1, t0 + 100)
    joined, unjoined = census.join_rows(process_rows, elapsed, census.record_files(tmp_path))
    assert len(joined) == 1 and len(unjoined) == 1


def test_file_outside_the_row_window_does_not_join(tmp_path):
    t0 = 4_000_000.0
    rows = [_process("x", t0, 800.0), _complete("x", t0, 1000.0)]
    _record(tmp_path, "late", t0 + 30.0, {"land_run.commit": 700.0})
    process_rows, elapsed = census.read_rows([_sink(tmp_path, rows)], t0 - 1, t0 + 100)
    joined, unjoined = census.join_rows(process_rows, elapsed, census.record_files(tmp_path))
    assert joined == [] and len(unjoined) == 1


def test_benchmark_origin_and_window_filter_rows(tmp_path):
    t0 = 5_000_000.0
    rows = [_process("p", t0, 900.0, origin="production"), _process("b", t0, 900.0, origin="benchmark"),
            _process("old", t0 - 10_000, 900.0)]
    process_rows, _ = census.read_rows([_sink(tmp_path, rows)], t0 - 1, t0 + 1)
    assert [r["corr_id"] for r in process_rows] == ["p"]


def test_census_totals_legs_and_unattributed(tmp_path, monkeypatch):
    t0 = 6_000_000.0
    rows = [_process("a", t0, 1000.0), _complete("a", t0, 2000.0),
            _process("b", t0 + 100, 2000.0), _complete("b", t0 + 100, 3000.0),
            _process("c", t0 + 200, 700.0)]
    sink = _sink(tmp_path, rows)
    _record(tmp_path, "run-a", t0 + 2.0, {"land_run.script_read": 100.0, "land_run.integrity": 800.0})
    _record(tmp_path, "run-b", t0 + 103.0, {"land_run.script_read": 100.0, "land_run.integrity": 1700.0})
    from coordinator_core.telemetry import op_latency
    import coordinator_core.lifecycle as lifecycle

    monkeypatch.setattr(op_latency, "sink_generations", lambda root: [sink])
    monkeypatch.setattr(lifecycle, "main_worktree_root", lambda root: tmp_path)
    out = census.census(repo_root=tmp_path, since=t0 - 1, until=t0 + 1000)
    assert out["over_bar_rows"] == 3 and out["joined"] == 2 and out["unjoined"] == 1
    assert out["joined"] + out["unjoined"] == out["over_bar_rows"]
    integrity = next(leg for leg in out["legs"] if leg["leg"] == "land_run.integrity")
    assert integrity["rows_over_bar"] == 2 and integrity["dominant_rows"] == 2
    assert integrity["max_ms"] == 1700.0 and integrity["p50_ms"] == 1250.0
    assert out["unattributed"] == {"rows": 2, "max_ms": 200.0, "p50_ms": 150.0}


def test_parse_time_forms():
    assert census.parse_time("2h", 10_000.0) == 10_000.0 - 7200.0
    assert census.parse_time("123.5", 0.0) == 123.5
    assert census.parse_time("2026-11-03T00:00:00+00:00", 0.0) == 1_793_664_000.0
