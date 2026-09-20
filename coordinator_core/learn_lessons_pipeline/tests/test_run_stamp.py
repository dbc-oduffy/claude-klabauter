"""Tests for coordinator_core.learn_lessons_pipeline.run_stamp.

Spec backlink: docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md § C2
"""

from __future__ import annotations

import pytest

from coordinator_core.learn_lessons_pipeline.run_stamp import stamp_run_complete
from coordinator_core.ops.learn_lessons_cutoff import derive_cutoff


def test_fresh_stamp_creates_dir_and_file(tmp_path):
    runs_dir = tmp_path / "tasks"
    sentinel = stamp_run_complete(runs_dir, "2026-09-19")

    assert sentinel == runs_dir / "learn-lessons-2026-09-19" / "COMPLETE"
    assert sentinel.is_file()
    assert sentinel.read_bytes() == b""


def test_restamp_is_a_noop(tmp_path):
    runs_dir = tmp_path / "tasks"
    sentinel = stamp_run_complete(runs_dir, "2026-09-19")
    first_stat = sentinel.stat()

    second = stamp_run_complete(runs_dir, "2026-09-19")
    second_stat = second.stat()

    assert second == sentinel
    assert second_stat.st_mtime == first_stat.st_mtime
    assert second_stat.st_size == first_stat.st_size == 0


def test_malformed_date_raises(tmp_path):
    runs_dir = tmp_path / "tasks"
    with pytest.raises(ValueError):
        stamp_run_complete(runs_dir, "2026-13-45-bogus")
    with pytest.raises(ValueError):
        stamp_run_complete(runs_dir, "not-a-date")
    assert not runs_dir.exists()


def test_roundtrip_with_derive_cutoff(tmp_path):
    runs_dir = tmp_path / "tasks"
    stamp_run_complete(runs_dir, "2026-09-19")

    assert derive_cutoff(runs_dir) == "2026-09-19"
