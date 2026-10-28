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


def test_main_rejects_dangling_op_flag(capsys):
    assert drv.main(["somedir", "--op"]) == 2


@pytest.mark.parametrize("argv", [[], ["a", "b"]])
def test_main_requires_one_dir(argv):
    assert drv.main(argv) == 2
