"""The run-dir scan benchmark returns its JSON shape at a small N; it asserts no timing."""

from __future__ import annotations

import json

from coordinator_core.benchmarks import warp_rundir_scan_budget as bench

N = 200


def test_report_shape_at_small_n(tmp_path) -> None:
    report = bench.run_benchmark(tmp_path, run_dirs=N)
    json.dumps(report)
    assert report["run_dirs"] == N
    assert tuple(report["axes"]) == bench.AXES
    for passes in report["axes"].values():
        assert tuple(passes) == bench.PASSES
        for row in passes.values():
            assert set(row) == {"process_ms", "read_bytes_calls", "read_text_calls", "result"}
            assert row["process_ms"] >= 0
            assert isinstance(row["read_bytes_calls"], int)


def test_cold_passes_read_the_run_dirs_and_the_fixture_is_n_runs(tmp_path) -> None:
    report = bench.run_benchmark(tmp_path, run_dirs=N)
    assert sum(1 for p in (tmp_path / "state" / "warp").iterdir() if p.is_dir()) == N
    axes = report["axes"]
    assert axes["manifest_declared"]["cold"]["read_bytes_calls"] >= N
    assert axes["halted_withheld_index"]["cold"]["result"] == N
    assert axes["ack_status"]["cold"]["result"] == N
