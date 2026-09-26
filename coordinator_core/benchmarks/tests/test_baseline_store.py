
from __future__ import annotations

from pathlib import Path

from coordinator_core.benchmarks import baseline_store
from coordinator_core.benchmarks.record import ConformanceRecord, Tolerance


def _make_record(**overrides) -> ConformanceRecord:
    fields = dict(
        op="coverage.gate",
        op_class="COMPUTE_ONLY",
        target_ms=150.0,
        tolerance=Tolerance(kind="relative", value=0.2),
        gating_statistic="min",
        gating_statistic_value=42.5,
        min=42.5,
        p50=48.0,
        p95=55.0,
        p99=60.0,
        sample_count=20,
        cold_start_floor_ms=59.0,
        floor_delta_ms=-16.5,
        floor_cov=0.05,
        floor_scope="run",
        run_id="run-abc123",
        code_sha="deadbeef",
        machine="windows-boxa",
    )
    fields.update(overrides)
    return ConformanceRecord(**fields)


def test_query_excludes_none_machine_records(tmp_path: Path) -> None:
    store_path = tmp_path / "store.jsonl"
    baseline_store.append(_make_record(machine=None), path=store_path)
    baseline_store.append(_make_record(machine="windows-boxa"), path=store_path)

    results = list(baseline_store.query(path=store_path))

    assert len(results) == 1
    assert results[0].machine == "windows-boxa"


def test_query_partitions_by_machine(tmp_path: Path) -> None:
    store_path = tmp_path / "store.jsonl"
    baseline_store.append(_make_record(machine="darwin-boxb"), path=store_path)
    baseline_store.append(_make_record(machine="windows-boxa"), path=store_path)

    results = list(baseline_store.query(machine="windows-boxa", path=store_path))

    assert len(results) == 1
    assert results[0].machine == "windows-boxa"


def test_runs_path_is_per_machine_under_runs_dir() -> None:
    path = baseline_store.runs_path(machine="windows-boxa")

    assert path == baseline_store.RUNS_DIR / "windows-boxa.jsonl"
    assert path.parent.name == "runs"


def test_tracked_baseline_path_is_directly_under_baselines_dir() -> None:
    """tracked_baseline_path() resolves to a file DIRECTLY under
    baselines/, never baselines/<machine>/... -- the shape the git
    negation in coordinator_core/.gitignore depends on."""
    path = baseline_store.tracked_baseline_path(machine="windows-boxa")

    assert path == baseline_store.BASELINES_DIR / "tracked-windows-boxa.jsonl"
    assert path.parent == baseline_store.BASELINES_DIR


def test_write_tracked_baseline_overwrites_not_appends(tmp_path: Path) -> None:
    path = tmp_path / "tracked-windows-boxa.jsonl"

    baseline_store.write_tracked_baseline([_make_record(op="ping")], path=path)
    first_read = baseline_store.read_tracked_baseline(path=path)
    assert [r.op for r in first_read] == ["ping"]

    baseline_store.write_tracked_baseline(
        [_make_record(op="coverage.gate")], path=path
    )
    second_read = baseline_store.read_tracked_baseline(path=path)

    assert [r.op for r in second_read] == ["coverage.gate"]


def test_write_tracked_baseline_orders_by_op(tmp_path: Path) -> None:
    path = tmp_path / "tracked-windows-boxa.jsonl"

    baseline_store.write_tracked_baseline(
        [_make_record(op="zzz.last"), _make_record(op="aaa.first")], path=path
    )

    ops = [r.op for r in baseline_store.read_tracked_baseline(path=path)]
    assert ops == ["aaa.first", "zzz.last"]


def test_read_tracked_baseline_empty_before_first_refresh(tmp_path: Path) -> None:
    path = tmp_path / "tracked-nonexistent.jsonl"

    assert baseline_store.read_tracked_baseline(path=path) == []
