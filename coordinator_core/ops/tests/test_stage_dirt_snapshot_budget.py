"""stage_dirt_snapshot.write_snapshot stays under 50ms process time however large the index and dirty count."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as snap
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

DIRTY = 400
CLEAN = 400
BUDGET_S = 0.050
# Ceiling for the 800-file fixture, not the 74k-entry real index: it guards against a regression to
# heavy per-file work, and the real-index figure comes from benchmarks/review_undeclared_edits_scan.py.
REVIEW_BUDGET_S = 0.200


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    )


@pytest.fixture
def dirty_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "src").mkdir()
    for i in range(DIRTY + CLEAN):
        (tmp_path / "src" / f"f{i}.py").write_text(f"v = {i}\n" * 40, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    for i in range(DIRTY):
        (tmp_path / "src" / f"f{i}.py").write_text(f"changed = {i}\n" * 60, encoding="utf-8")
    return tmp_path


def test_stage_under_budget_and_independent_of_index_size(dirty_repo, tmp_path_factory, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("stage walked the index")

    monkeypatch.setattr("coordinator_core.git.git_index.parse_index_identity", boom)
    monkeypatch.setattr("coordinator_core.git.git_index.parse_index_stat", boom)
    records = tmp_path_factory.mktemp("records")
    snap.write_snapshot(dirty_repo, records / "warm")
    best = None
    for i in range(3):
        start = time.process_time()
        written = snap.write_snapshot(dirty_repo, records / f"r{i}")
        elapsed = time.process_time() - start
        assert written is not None
        best = elapsed if best is None else min(best, elapsed)
    assert best < BUDGET_S, f"stage snapshot took {best * 1000:.1f}ms process time over {DIRTY} dirty files"


def test_review_scan_under_200ms_process_time(dirty_repo, tmp_path_factory):
    record = tmp_path_factory.mktemp("records") / "run"
    snap.write_snapshot(dirty_repo, record)
    best = None
    for _ in range(3):
        start = time.process_time()
        snap.undeclared_edits(dirty_repo, record, [])
        elapsed = time.process_time() - start
        best = elapsed if best is None else min(best, elapsed)
    assert best < REVIEW_BUDGET_S, f"review scan took {best * 1000:.1f}ms process time"


def test_review_scan_flags_only_bytes_changed_after_stage(dirty_repo, tmp_path_factory):
    aged = time.time() - 3600
    for i in range(DIRTY):
        os.utime(dirty_repo / "src" / f"f{i}.py", (aged, aged))
    record = tmp_path_factory.mktemp("records") / "run"
    snap.write_snapshot(dirty_repo, record)
    assert snap.undeclared_edits(dirty_repo, record, []) == []
    (dirty_repo / "src" / "f7.py").write_text("changed after stage\n", encoding="utf-8")
    assert snap.undeclared_edits(dirty_repo, record, []) == ["src/f7.py"]
