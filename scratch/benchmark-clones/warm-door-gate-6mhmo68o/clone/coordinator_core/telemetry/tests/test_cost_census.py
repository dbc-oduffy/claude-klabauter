"""Tests for coordinator_core.telemetry.cost_census — see that module's
docstring for the measured-axes rationale and cadence justification this
pins (spec backlink:
state/handoffs/2026-08-15-kill-it-if-it-cannot-pay-for-itself.md AC5)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.telemetry import cost_census


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


def test_run_census_summarizes_hot_path_op_and_is_comparable_across_runs(
    monkeypatch, tmp_path
):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    now = time.time()
    op = cost_census.HOT_PATH_OPS[0]
    rows = [
        {"op": op, "t_start": now - 10, "elapsed_ms": 100.0, "kind": "complete"},
        {"op": op, "t_start": now - 5, "elapsed_ms": 300.0, "kind": "complete"},
        {"op": "some.other.op", "t_start": now - 5, "elapsed_ms": 999.0, "kind": "complete"},
    ]
    _write_jsonl(sink, rows)

    row1 = cost_census.run_census(repo_root=tmp_path, now=now, write=True)
    assert row1["hot_path_ops"][op]["n"] == 2
    assert row1["hot_path_ops"][op]["max_ms"] == 300.0
    assert row1["rows_matched"] == 2  # "some.other.op" not in the tracked set

    # A second run appends, never overwrites — the series is comparable.
    row2 = cost_census.run_census(repo_root=tmp_path, now=now + 1, write=True)
    series_path = tmp_path / "state" / "cost-census.jsonl"
    lines = series_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["t_run"] == row1["t_run"]
    assert json.loads(lines[1])["t_run"] == row2["t_run"]


def test_run_census_excludes_rows_outside_the_lookback_window(monkeypatch, tmp_path):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    now = time.time()
    op = cost_census.HOT_PATH_OPS[0]
    rows = [
        {"op": op, "t_start": now - 2 * 24 * 3600, "elapsed_ms": 100.0, "kind": "complete"},
        {"op": op, "t_start": now - 60, "elapsed_ms": 200.0, "kind": "complete"},
    ]
    _write_jsonl(sink, rows)

    row = cost_census.run_census(repo_root=tmp_path, now=now, write=False)
    assert row["hot_path_ops"][op]["n"] == 1
    assert row["hot_path_ops"][op]["max_ms"] == 200.0


def test_run_census_never_raises_on_missing_sink(tmp_path, monkeypatch):
    _patch_repo(monkeypatch, tmp_path)
    row = cost_census.run_census(repo_root=tmp_path, write=False)
    for op in cost_census.HOT_PATH_OPS:
        assert row["hot_path_ops"][op]["n"] == 0
    assert row["truncated"] is False


def test_run_census_reads_rotated_generations_too(monkeypatch, tmp_path):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    logs_dir = common_dir / "coordinator-sessions" / "logs"
    now = time.time()
    op = cost_census.HOT_PATH_OPS[0]
    _write_jsonl(
        logs_dir / "op-latency.jsonl",
        [{"op": op, "t_start": now - 5, "elapsed_ms": 50.0, "kind": "complete"}],
    )
    _write_jsonl(
        logs_dir / "op-latency.1.jsonl",
        [{"op": op, "t_start": now - 6, "elapsed_ms": 75.0, "kind": "complete"}],
    )

    row = cost_census.run_census(repo_root=tmp_path, now=now, write=False)
    assert row["hot_path_ops"][op]["n"] == 2


def test_run_census_truncates_rather_than_reading_unbounded(monkeypatch, tmp_path):
    common_dir = _patch_repo(monkeypatch, tmp_path)
    sink = common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    now = time.time()
    op = cost_census.HOT_PATH_OPS[0]
    rows = [{"op": op, "t_start": now - 1, "elapsed_ms": 1.0, "kind": "complete"} for _ in range(10)]
    _write_jsonl(sink, rows)

    row = cost_census.run_census(repo_root=tmp_path, now=now, write=False, max_rows=3)
    assert row["truncated"] is True
    assert row["rows_scanned"] == 3


def test_run_census_row_is_self_bounded_and_reports_own_cost(tmp_path, monkeypatch):
    _patch_repo(monkeypatch, tmp_path)
    row = cost_census.run_census(repo_root=tmp_path, write=False)
    assert isinstance(row["census_elapsed_ms"], float)
    assert row["census_elapsed_ms"] >= 0.0
