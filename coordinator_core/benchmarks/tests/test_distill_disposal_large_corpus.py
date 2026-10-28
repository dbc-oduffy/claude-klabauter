"""Tests for the distill disposal large-corpus bench: corpus shape and a small end-to-end run."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import distill_disposal_large_corpus as bench

# Spawns real git/python processes; runs at cadence gates, not per-commit.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_build_corpus_meets_requested_shape(tmp_path):
    built = bench.build_corpus(tmp_path / "c", files=120, candidates=10, cited=3)
    assert len(built["eligible"]) == 10
    assert len(built["candidates"]) == 13
    n_files = sum(1 for p in (tmp_path / "c").rglob("*.md"))
    assert n_files >= 120


def test_default_scale_matches_issue_72():
    assert bench.DEFAULT_FILES >= 5000
    assert bench.DEFAULT_CANDIDATES >= 200


def test_run_reports_process_time_and_spawns_for_both_ops(tmp_path):
    rows = bench.run(files=60, candidates=5, base_dir=tmp_path)
    assert [r["op"] for r in rows] == [
        "distill.assemble_disposal_manifest",
        "distill.apply_disposal",
    ]
    for r in rows:
        assert r["ok"], r
        assert r["process_ms"] > 0
        assert r["spawns"] >= 0
