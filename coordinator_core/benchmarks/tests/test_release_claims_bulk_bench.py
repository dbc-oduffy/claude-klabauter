"""The release-claims bench's JSON contract at a small claim count; values are not asserted."""
from __future__ import annotations

import json

from coordinator_core import benchmarks
from coordinator_core.benchmarks import release_claims_bulk_bench as bench

ROW_KEYS = {"process_ms", "spawn_count"}


def test_json_carries_process_ms_and_spawn_count_for_every_axis(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(benchmarks, "declare_benchmark_origin", lambda: None)
    bench.main(["--claims", "50", "--json", "--scratch-dir", str(tmp_path)])
    doc = json.loads(capsys.readouterr().out)
    for axis in bench.AXES:
        assert ROW_KEYS <= set(doc[axis]), axis
    assert "claim_index_falls_back_to_full_scan" in doc["dead_owner_blocking"]
    assert doc["per_path_remedy"]["calls"] == 50


def test_full_release_retains_no_own_claim_absolute_agent_sink_entries_included(tmp_path):
    for kwargs in ({}, {"absolute": False}, {"ineligible": True}):
        row = bench.measure_full_release(60, str(tmp_path), **kwargs)
        assert row["error"] is None, kwargs
        assert row["held_before"] == 60, kwargs
        assert row["retained_after"] == 0, kwargs


def test_full_release_spawn_count_does_not_grow_with_claims(tmp_path):
    small = bench.measure_full_release(40, str(tmp_path))
    large = bench.measure_full_release(400, str(tmp_path))
    assert large["spawn_count"] <= small["spawn_count"]


def test_ended_owner_claims_never_contest_and_spawns_do_not_grow(tmp_path):
    counts = {n: bench.measure_dead_owner_blocking(n, str(tmp_path)) for n in (20, 400)}
    for n, row in counts.items():
        assert row["blocking_paths"] == 0, n
    assert counts[400]["spawn_count"] <= counts[20]["spawn_count"]
