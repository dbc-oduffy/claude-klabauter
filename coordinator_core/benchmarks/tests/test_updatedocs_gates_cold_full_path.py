"""Tests of the updatedocs cold-path bench driver: fixture shape and the write-count diff."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core.benchmarks import updatedocs_gates_cold_full_path as drv


def test_diff_counts_added_changed_and_removed():
    before = {"a": "1", "b": "2", "c": "3"}
    after = {"a": "1", "b": "9", "d": "4"}
    assert drv.diff_snapshots(before, after) == (2, 1)


def test_identical_snapshots_write_nothing():
    snap = {"a": "1"}
    assert drv.diff_snapshots(snap, dict(snap)) == (0, 0)


def test_fixture_has_resolved_queue_line_and_stale_sidecar(tmp_path):
    repo = drv.ensure_fixture(tmp_path)
    assert "resolution: resolved" in (repo / drv.QUEUE_REL).read_text(encoding="utf-8")
    sidecar = repo / drv.SIDECAR_REL
    age_days = (time.time() - os.stat(sidecar).st_mtime) / 86400
    assert age_days > 14
    assert (repo / ".git").is_dir()


def test_snapshot_excludes_git_dir(tmp_path):
    repo = drv.ensure_fixture(tmp_path)
    snap = drv.snapshot(repo)
    assert drv.QUEUE_REL in snap and drv.SIDECAR_REL in snap
    assert not any(k.startswith(".git/") for k in snap)


def test_large_fixture_commits_plans_handoffs_and_tasks(tmp_path):
    repo = drv.ensure_fixture(tmp_path, plans=12, handoffs=5)
    tracked = drv.run_git(["ls-files", "-z"], cwd=str(repo)).stdout.split("\0")
    assert sum(1 for t in tracked if t.startswith("docs/plans/")) == 12
    assert sum(1 for t in tracked if t.startswith("archive/handoffs/")) == 5
    assert sum(1 for t in tracked if t.startswith("tasks/")) == 5
    assert drv.run_git(["status", "--porcelain", "--untracked-files=no"], cwd=str(repo)).stdout.strip() == ""


def test_small_fixture_has_no_plans_or_tasks(tmp_path):
    repo = drv.ensure_fixture(tmp_path)
    assert not (repo / "docs").exists() and not (repo / "tasks").exists()


def test_cli_defaults_to_the_thousand_plan_fixture(monkeypatch, tmp_path):
    seen = {}

    def fake_measure(op, parent, plans=0, handoffs=0):
        seen.update(op=op, plans=plans, handoffs=handoffs)
        return {"has_result": True, "envelope_code": None}

    monkeypatch.setattr(drv, "measure", fake_measure)
    monkeypatch.setattr("coordinator_core.benchmarks.declare_benchmark_origin", lambda: None)
    assert drv.main([str(tmp_path)]) == 0
    assert seen == {"op": "updatedocs.gates", "plans": 1000, "handoffs": 300}
    assert drv.main([str(tmp_path), "--op", "refresh-queries", "--plans", "7", "--handoffs", "2"]) == 0
    assert seen == {"op": "refresh-queries", "plans": 7, "handoffs": 2}


def test_main_rejects_non_numeric_plans():
    assert drv.main(["somedir", "--plans", "many"]) == 2


def test_refresh_queries_child_reports_metrics_on_small_fixture(tmp_path):
    report = drv.measure(drv.REFRESH_OP, tmp_path, plans=3, handoffs=2)
    assert report["op_class"] == "cli"
    assert report["has_result"] is True
    assert isinstance(report["spawns"], int) and report["ls_files_spawns"] is not None


def test_main_rejects_dangling_op_flag(capsys):
    assert drv.main(["somedir", "--op"]) == 2


@pytest.mark.parametrize("argv", [[], ["a", "b"]])
def test_main_requires_one_dir(argv):
    assert drv.main(argv) == 2
