"""`dispatch.land_run` on a halted run costs the same spawns and under the 500ms bar however many run dirs or withheld paths exist."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import fixture_settings_home
from coordinator_core.benchmarks import land_run_halt_path_profile as prof

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SMALL_RUN_DIRS = 30
LARGE_RUN_DIRS = 3000
WITHHELD_PATHS = 130


@pytest.fixture(autouse=True)
def _session_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", prof.SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _halt_row(tmp_path, run_dirs: int, name: str, *, withheld_paths: int = 0):
    workdir = tmp_path / name
    workdir.mkdir()
    with fixture_settings_home(workdir):
        root, _run_id, params = prof.build_fixture(workdir, run_dirs, withheld_paths)
        return prof.measure(root, params, run_dirs, withheld_paths=withheld_paths)


def test_halt_path_spawns_do_not_grow_with_run_dirs_and_stay_under_the_bar(tmp_path):
    _halt_row(tmp_path, prof.WARMUP_RUN_DIRS, "warm")  # first call pays module imports, not the halt path
    small = _halt_row(tmp_path, SMALL_RUN_DIRS, "small")
    large = _halt_row(tmp_path, LARGE_RUN_DIRS, "large")

    assert small.outcome == large.outcome, (small.outcome, large.outcome, large.attribution)
    assert large.committed is small.committed
    assert large.spawns == small.spawns, (small.spawns, large.spawns)
    assert large.process_time_ms < prof.BAR_MS, large


def test_halt_path_over_a_130_path_withheld_set_keeps_spawns_flat_and_stays_under_the_bar(tmp_path):
    _halt_row(tmp_path, prof.WARMUP_RUN_DIRS, "warm")
    bare = _halt_row(tmp_path, SMALL_RUN_DIRS, "bare")
    wide = _halt_row(tmp_path, SMALL_RUN_DIRS, "wide", withheld_paths=WITHHELD_PATHS)

    assert wide.reply_returned and wide.withheld_paths == WITHHELD_PATHS, wide
    assert wide.outcome == bare.outcome, (bare.outcome, wide.outcome, wide.attribution)
    assert wide.spawns == bare.spawns, (bare.spawns, wide.spawns)
    assert wide.process_time_ms < prof.BAR_MS, wide


def test_the_stall_seam_blocks_the_first_read_once_and_restores_the_primitives():
    from pathlib import Path

    from coordinator_core import bounded_io
    from coordinator_core.git import call_io_scope

    before = (call_io_scope._read, bounded_io._read_registered, Path.read_bytes)
    with prof.stalled_first_read(1) as stall:
        assert stall is not None and stall.end is None
        call_io_scope.read_bytes(__file__)
        first_end = stall.end
        call_io_scope.read_bytes(__file__ + ".absent")
        assert first_end is not None and stall.end == first_end and stall.path == __file__
    assert (call_io_scope._read, bounded_io._read_registered, Path.read_bytes) == before
    with prof.stalled_first_read(0) as none:
        assert none is None
