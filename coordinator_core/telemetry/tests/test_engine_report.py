"""Tests for coordinator_core.telemetry.engine_report — the supported,
rotation-aware read surface promoted from cost_census.py's private
mechanism (spec backlink:
docs/plans/2026-08-19-warm-engine-gets-an-honest-instrument.md C1)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from coordinator_core.telemetry import engine_report


def _git_common_dir(tmp_path: Path) -> Path:
    common = tmp_path / ".git"
    common.mkdir()
    return common


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _patch_repo(monkeypatch, tmp_path: Path):
    common_dir = _git_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir
    )
    return common_dir


def test_latency_percentiles_and_per_op_over_synthetic_corpus(monkeypatch, tmp_path):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    now = time.time()
    rows = [
        {"op": "op.a", "t_start": now - 10, "elapsed_ms": 100.0, "kind": "complete"},
        {"op": "op.a", "t_start": now - 9, "elapsed_ms": 200.0, "kind": "complete"},
        {"op": "op.a", "t_start": now - 8, "elapsed_ms": 300.0, "kind": "complete"},
        {"op": "op.b", "t_start": now - 7, "elapsed_ms": 900.0, "kind": "complete"},
        # a "started" row must never contribute a latency value
        {"op": "op.a", "t_start": now - 6, "kind": "started", "corr_id": "x"},
    ]
    _write_jsonl(sink, rows)

    entries = list(engine_report.iter_sink_entries(repo_root=tmp_path))
    overall = engine_report.latency_percentiles(entries)
    assert overall["n"] == 4
    assert overall["max_ms"] == 900.0
    assert overall["p50_ms"] is not None

    per_op = engine_report.per_op_latency(entries)
    assert set(per_op.keys()) == {"op.a", "op.b"}
    assert per_op["op.a"]["n"] == 3
    assert per_op["op.a"]["max_ms"] == 300.0
    assert per_op["op.b"]["n"] == 1
    assert per_op["op.b"]["max_ms"] == 900.0

    # "some.other.op" style hot-path-only limiting must NOT apply here —
    # every op present shows up, unlike cost_census.HOT_PATH_OPS.
    assert "op.a" in per_op and "op.b" in per_op


def test_iter_sink_entries_spans_rotated_generations(monkeypatch, tmp_path):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    logs_dir = common_dir / "coordinator-sessions" / "logs"
    now = time.time()
    _write_jsonl(
        logs_dir / "op-latency.jsonl",
        [{"op": "op.live", "t_start": now - 1, "elapsed_ms": 10.0, "kind": "complete"}],
    )
    _write_jsonl(
        logs_dir / "op-latency.1.jsonl",
        [{"op": "op.old", "t_start": now - 100, "elapsed_ms": 20.0, "kind": "complete"}],
    )

    # Reading only the live file would have missed the older generation.
    live_only = list(
        engine_report.iter_sink_entries(
            sink_paths=[logs_dir / "op-latency.jsonl"]
        )
    )
    assert {e["op"] for e in live_only} == {"op.live"}

    all_entries = list(engine_report.iter_sink_entries(repo_root=tmp_path))
    assert {e["op"] for e in all_entries} == {"op.live", "op.old"}

    per_op = engine_report.per_op_latency(all_entries)
    assert per_op["op.old"]["n"] == 1
    assert per_op["op.live"]["n"] == 1


def test_iter_sink_entries_never_raises_on_malformed_lines(monkeypatch, tmp_path):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    sink.parent.mkdir(parents=True, exist_ok=True)
    sink.write_text(
        '{"op": "ok.op", "t_start": 1.0, "elapsed_ms": 5.0, "kind": "complete"}\n'
        "not json at all\n"
        "\n",
        encoding="utf-8",
    )

    entries = list(engine_report.iter_sink_entries(repo_root=tmp_path))
    assert len(entries) == 1
    assert entries[0]["op"] == "ok.op"


def test_iter_sink_entries_missing_sink_yields_nothing(monkeypatch, tmp_path):
    _patch_repo(monkeypatch, tmp_path)
    entries = list(engine_report.iter_sink_entries(repo_root=tmp_path))
    assert entries == []


def test_percentile_takes_fractions_not_percentiles():
    vals = [float(i) for i in range(1, 101)]  # 1..100
    assert engine_report._percentile(vals, 0.50) == 51.0
    # Passing 95 (not 0.95) silently returns the max — documented gotcha.
    assert engine_report._percentile(vals, 95) == 100.0


def test_route_distribution_unstamped_never_lands_in_by_route():
    # DR-328: an unstamped row (route is None) is UNOBSERVABLE, NOT COLD.
    entries = [
        {"op": "op.a", "route": "warm_server", "kind": "complete"},
        {"op": "op.a", "route": "warm_server", "kind": "complete"},
        {"op": "op.a", "route": "in_process", "kind": "complete"},
        {"op": "op.b", "route": None, "kind": "complete"},
        {"op": "op.b", "kind": "complete"},  # route key absent entirely
        {"op": "op.c", "route": "warm_server", "kind": "started"},  # not complete
    ]
    result = engine_report.route_distribution(entries, coverage_floor=0.5)

    assert result["complete"] == 5
    assert result["routed"] == 3
    assert result["unstamped"] == 2
    assert result["by_route"] == {"warm_server": 2, "in_process": 1}
    assert "unstamped" not in result["by_route"]

    # coverage reported separately from share, and share excludes unstamped.
    assert result["coverage"] == 3 / 5
    assert result["warm_share_of_routed"] == 2 / 3
    assert result["verdict"] == "ok"


def test_route_distribution_at_real_corpus_coverage_is_unknown():
    # Today's measured live-corpus coverage: 413/160,861 = 0.257% (DR-328).
    complete_rows = [{"op": "op.a", "kind": "complete"} for _ in range(160_861 - 413)]
    routed_rows = [
        {"op": "op.a", "route": "warm_server", "kind": "complete"} for _ in range(413)
    ]
    entries = complete_rows + routed_rows

    result = engine_report.route_distribution(entries)

    assert result["complete"] == 160_861
    assert result["routed"] == 413
    assert abs(result["coverage"] - 413 / 160_861) < 1e-9
    # share is still reported even though the verdict can't rely on it.
    assert result["warm_share_of_routed"] == 1.0
    assert result["verdict"] == "unknown"
    assert "coverage" in result["verdict_reason"]


def test_route_distribution_empty_corpus_is_unknown_not_crash():
    result = engine_report.route_distribution([])
    assert result["complete"] == 0
    assert result["routed"] == 0
    assert result["warm_share_of_routed"] is None
    assert result["verdict"] == "unknown"


def test_route_distribution_default_min_complete_rows_is_back_compat():
    # An existing-shaped call (no min_complete_rows) is byte-identical.
    entries = [
        {"op": "op.a", "route": "warm_server", "kind": "complete"},
        {"op": "op.a", "route": "in_process", "kind": "complete"},
    ]
    with_default = engine_report.route_distribution(entries, coverage_floor=0.5)
    without_default = engine_report.route_distribution(
        entries, coverage_floor=0.5, min_complete_rows=0
    )
    assert with_default == without_default


def test_route_distribution_refuses_below_min_complete_rows():
    entries = [
        {"op": "op.a", "route": "warm_server", "kind": "complete"},
        {"op": "op.a", "route": "in_process", "kind": "complete"},
        {"op": "op.a", "route": "warm_server", "kind": "complete"},
    ]
    result = engine_report.route_distribution(
        entries, coverage_floor=0.0, min_complete_rows=50
    )
    assert result["complete"] == 3
    assert result["verdict"] == "unknown"
    assert "3" in result["verdict_reason"]
    assert "50" in result["verdict_reason"]


def test_route_distribution_passes_at_exactly_min_complete_rows():
    entries = [
        {"op": "op.a", "route": "warm_server", "kind": "complete"}
        for _ in range(3)
    ]
    result = engine_report.route_distribution(
        entries, coverage_floor=0.0, min_complete_rows=3
    )
    assert result["complete"] == 3
    assert result["verdict"] == "ok"


def test_route_distribution_row_count_and_coverage_reasons_are_distinguishable():
    entries = [
        {"op": "op.a", "route": "warm_server", "kind": "complete"}
        for _ in range(3)
    ]
    below_min = engine_report.route_distribution(
        entries, coverage_floor=0.0, min_complete_rows=50
    )
    below_coverage = engine_report.route_distribution(
        [{"op": "op.a", "kind": "complete"} for _ in range(100)]
        + [{"op": "op.a", "route": "warm_server", "kind": "complete"}],
        coverage_floor=0.5,
        min_complete_rows=0,
    )
    assert below_min["verdict"] == "unknown"
    assert below_coverage["verdict"] == "unknown"
    assert below_min["verdict_reason"] != below_coverage["verdict_reason"]
    assert "min_complete_rows" in below_min["verdict_reason"]
    assert "coverage" in below_coverage["verdict_reason"]


def test_route_distribution_zero_rows_refuses_under_both_minimums():
    # A 0-row window already refuses via the coverage floor's
    # `if complete else 0.0` guard regardless of the row-count minimum —
    # the one input where the two guards overlap. At min_complete_rows=0
    # the row-count check never fires (0 < 0 is false), so the coverage
    # reason is the one that surfaces; at min_complete_rows=50 the
    # row-count check fires first (row-count runs BEFORE coverage), so
    # that reason surfaces instead. Pin which reason fires at each.
    result_default = engine_report.route_distribution(
        [], coverage_floor=0.05, min_complete_rows=0
    )
    assert result_default["complete"] == 0
    assert result_default["verdict"] == "unknown"
    assert "coverage" in result_default["verdict_reason"]
    assert "min_complete_rows" not in result_default["verdict_reason"]

    result_with_floor = engine_report.route_distribution(
        [], coverage_floor=0.05, min_complete_rows=50
    )
    assert result_with_floor["complete"] == 0
    assert result_with_floor["verdict"] == "unknown"
    assert "min_complete_rows" in result_with_floor["verdict_reason"]


def test_process_fanout_overall_and_per_op():
    entries = [
        {"op": "op.a", "pid": 100, "kind": "complete"},
        {"op": "op.a", "pid": 100, "kind": "complete"},
        {"op": "op.a", "pid": 200, "kind": "complete"},
        {"op": "op.b", "pid": 200, "kind": "complete"},
        {"op": "op.b", "pid": 300, "kind": "complete"},
        {"op": "op.c", "pid": 400, "kind": "started"},  # not complete, excluded
    ]
    result = engine_report.process_fanout(entries)

    assert result["ops"] == 5
    assert result["distinct_pids"] == 3
    assert result["ops_per_pid"] == 5 / 3
    assert result["by_op"]["op.a"] == {"ops": 3, "distinct_pids": 2, "ops_per_pid": 1.5}
    assert result["by_op"]["op.b"] == {"ops": 2, "distinct_pids": 2, "ops_per_pid": 1.0}
    assert "op.c" not in result["by_op"]


def test_process_fanout_empty_corpus_is_zero_not_crash():
    result = engine_report.process_fanout([])
    assert result == {"ops": 0, "distinct_pids": 0, "ops_per_pid": 0.0, "by_op": {}}


def test_outcome_split_unknown_outcome_lands_in_other():
    entries = [
        {"op": "op.a", "outcome": "ok", "kind": "complete"},
        {"op": "op.a", "outcome": "ok", "kind": "complete"},
        {"op": "op.a", "outcome": "error", "kind": "complete"},
        {"op": "op.b", "outcome": "timeout", "kind": "complete"},
        {"op": "op.b", "outcome": "partial_mutation", "kind": "complete"},  # unrecognised
        {"op": "op.c", "outcome": "ok", "kind": "started"},  # not complete, excluded
    ]
    result = engine_report.outcome_split(entries)

    assert result["overall"] == {"ok": 2, "error": 1, "timeout": 1, "other": 1}
    assert result["by_op"]["op.a"] == {"ok": 2, "error": 1, "timeout": 0, "other": 0}
    assert result["by_op"]["op.b"] == {"ok": 0, "error": 0, "timeout": 1, "other": 1}
    assert "op.c" not in result["by_op"]


def test_engine_telemetry_report_composes_every_section(monkeypatch, tmp_path):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    now = time.time()
    rows = [
        {
            "op": "op.a", "t_start": now - 5, "elapsed_ms": 100.0,
            "kind": "complete", "pid": 111, "outcome": "ok",
        },
        {
            "op": "op.a", "t_start": now - 4, "elapsed_ms": 150.0,
            "kind": "complete", "pid": 111, "outcome": "ok",
        },
        {
            "op": "op.b", "t_start": now - 3, "elapsed_ms": 50.0,
            "kind": "complete", "pid": 222, "outcome": "error",
        },
    ]
    _write_jsonl(sink, rows)

    report = engine_report.engine_telemetry_report(repo_root=tmp_path)

    assert report["latency"]["n"] == 3
    assert set(report["per_op_latency"].keys()) == {"op.a", "op.b"}
    assert report["route_distribution"]["complete"] == 3
    assert report["process_fanout"]["ops"] == 3
    assert report["process_fanout"]["distinct_pids"] == 2
    assert report["outcome_split"]["overall"]["ok"] == 2
    assert report["outcome_split"]["overall"]["error"] == 1
    assert "pairing_summary" in report
    assert report["pairing_summary"]["total"] == 0


def test_warm_share_since_buckets_routed_rows_since_publish_stamp():
    since_ts = 1000.0
    entries = [
        # before the publish stamp — excluded entirely.
        {"route": "warm_server", "t_start": 999.0, "kind": "complete"},
        # bucket 0: [1000, 2800)
        {"route": "warm_server", "t_start": 1000.0, "kind": "complete"},
        {"route": "in_process", "t_start": 1500.0, "kind": "complete"},
        # bucket 1: [2800, 4600)
        {"route": "warm_server", "t_start": 2900.0, "kind": "complete"},
        {"route": "warm_server", "t_start": 3000.0, "kind": "complete"},
        # unstamped row in range — must not land in any bucket.
        {"route": None, "t_start": 1200.0, "kind": "complete"},
        {"t_start": 1200.0, "kind": "complete"},
        # non-complete row in range — excluded.
        {"route": "warm_server", "t_start": 1200.0, "kind": "started"},
    ]

    buckets = engine_report.warm_share_since(entries, since_ts=since_ts, bucket_secs=1800)

    assert set(buckets.keys()) == {0, 1}
    assert buckets[0]["routed"] == 2
    assert buckets[0]["warm"] == 1
    assert buckets[0]["warm_share"] == 0.5
    assert buckets[1]["routed"] == 2
    assert buckets[1]["warm"] == 2
    assert buckets[1]["warm_share"] == 1.0
