"""review_prep_dirty_worktree --opl arm: filters, window, percentiles and leg histogram over a fake sink."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.benchmarks import review_prep_dirty_worktree as bench


def _row(ms, t=100.0, op="review.prep", scope="per_op_handler", kind="process_time", **extra):
    return {"kind": kind, "op": op, "measurement_scope": scope, "process_ms": ms, "t_start": t, **extra}


def _fake_sink(tmp_path: Path, monkeypatch, rows_by_file):
    paths = []
    for i, rows in enumerate(rows_by_file):
        p = tmp_path / f"sink{i}.jsonl"
        p.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n", encoding="utf-8")
        paths.append(p)
    from coordinator_core.telemetry import op_latency

    monkeypatch.setattr(op_latency, "sink_generations", lambda repo_root: paths)


def test_opl_filters_scope_op_kind_and_counts_over_bar(tmp_path, monkeypatch):
    _fake_sink(tmp_path, monkeypatch, [
        [_row(100.0), _row(600.0), _row(2000.0, legs=[{"name": "a", "ms": 1}, {"name": "b", "ms": 2}]),
         _row(9999.0, scope="per_call"), _row(9999.0, op="other.op"), _row(9999.0, kind="wall"),
         "not json review.prep"],
        [_row(700.0, legs=[{"name": "a", "ms": 5}]), _row(50.0, legs=[{"name": "z"}])],
    ])
    got = bench.measure_opl(tmp_path)
    assert got["sinks"] == 2
    assert got["calls"] == 5
    assert got["over_bar"] == 3
    assert got["process_ms_max"] == 2000.0
    assert got["process_ms_p50"] == 600.0
    assert got["legs_histogram"] == {"a": 2, "b": 1}


def test_opl_since_window_excludes_older_and_untimed_rows(tmp_path, monkeypatch):
    _fake_sink(tmp_path, monkeypatch, [[_row(900.0, t=50.0), _row(800.0, t=150.0), _row(700.0, t=None)]])
    got = bench.measure_opl(tmp_path, since_epoch=100.0)
    assert got["calls"] == 1 and got["over_bar"] == 1 and got["process_ms_max"] == 800.0
    assert bench.measure_opl(tmp_path)["calls"] == 3


def test_opl_empty_sink_reports_none(tmp_path, monkeypatch):
    _fake_sink(tmp_path, monkeypatch, [[]])
    got = bench.measure_opl(tmp_path)
    assert got["calls"] == 0 and got["process_ms_max"] is None and got["process_ms_p50"] is None


def test_main_opl_prints_json_and_skips_seeded_arms(tmp_path, monkeypatch, capsys):
    _fake_sink(tmp_path, monkeypatch, [[_row(800.0)]])
    monkeypatch.setattr(bench, "measure", lambda *a, **k: (_ for _ in ()).throw(AssertionError("seeded arm ran")))
    assert bench.main(["--opl", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["calls"] == 1 and out["over_bar"] == 1 and out["legs_histogram"] == {}
