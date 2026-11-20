"""`dispatch.land_run` on a halted run costs the same spawns and under the 500ms bar however many run dirs exist."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import fixture_settings_home
from coordinator_core.benchmarks import land_run_halt_path_profile as prof

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SMALL_RUN_DIRS = 30
LARGE_RUN_DIRS = 3000


@pytest.fixture(autouse=True)
def _session_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", prof.SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _halt_row(tmp_path, run_dirs: int, name: str):
    workdir = tmp_path / name
    workdir.mkdir()
    with fixture_settings_home(workdir):
        root, _run_id, params = prof.build_fixture(workdir, run_dirs)
        return prof.measure(root, params, run_dirs)


def test_halt_path_spawns_do_not_grow_with_run_dirs_and_stay_under_the_bar(tmp_path):
    _halt_row(tmp_path, prof.WARMUP_RUN_DIRS, "warm")  # first call pays module imports, not the halt path
    small = _halt_row(tmp_path, SMALL_RUN_DIRS, "small")
    large = _halt_row(tmp_path, LARGE_RUN_DIRS, "large")

    assert small.outcome == large.outcome, (small.outcome, large.outcome, large.attribution)
    assert large.committed is small.committed
    assert large.spawns == small.spawns, (small.spawns, large.spawns)
    assert large.process_time_ms < prof.BAR_MS, large
