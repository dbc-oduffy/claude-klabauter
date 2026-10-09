"""DR-344 budget pins for plan_chain.run on the completed-chain fixture, with the fake runner."""
from __future__ import annotations

import pathlib
import subprocess
import time

import pytest

from coordinator_core.ops.plan_chain import driver
from coordinator_core.ops.plan_chain.tests._chain_repo import chain  # noqa: F401 -- fixture

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BAR_S = 0.5
_PACKAGE = pathlib.Path(driver.__file__).resolve().parent

# Measured at implementation: git spawns from dispatch.emit and terminal_commit (the fixture's
# Phase-1 stubs spawn nothing). A change means a spawn joined the chain's hot path.
_EXPECTED_POPEN = 5


def _run_chain(chain, monkeypatch):
    stage_times: dict[str, float] = {}

    def timed(stage, fn):
        def wrapper(*args, **kwargs):
            start = time.process_time()
            try:
                return fn(*args, **kwargs)
            finally:
                stage_times[stage] = stage_times.get(stage, 0.0) + (time.process_time() - start)

        return wrapper

    monkeypatch.setattr(driver.plan_stage, "bind_plan_script", timed("bind", driver.plan_stage.bind_plan_script))
    monkeypatch.setattr(driver.phase1_gates, "run", timed("phase1_gates", driver.phase1_gates.run))
    monkeypatch.setattr(driver.phase1_checks, "run", timed("phase1_checks", driver.phase1_checks.run))
    monkeypatch.setattr(driver.emit_leg, "run", timed("emit_leg", driver.emit_leg.run))
    real_commit = driver._engine_terminal_commit(str(chain.root))
    monkeypatch.setattr(
        driver, "_engine_terminal_commit", lambda repo_root: timed("terminal_commit", real_commit)
    )

    spawns = 0
    real_init = subprocess.Popen.__init__

    def counting_init(self, *args, **kwargs):
        nonlocal spawns
        spawns += 1
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting_init)
    digest = driver.run(chain.manifest, runner=chain.make_runner())
    monkeypatch.setattr(subprocess.Popen, "__init__", real_init)
    assert digest["chain"]["halted_at"] is None and digest["chain"]["commit"], digest["chain"]
    return spawns, stage_times


def test_popen_count_is_pinned(chain, monkeypatch):
    spawns, _times = _run_chain(chain, monkeypatch)
    assert spawns == _EXPECTED_POPEN, f"expected {_EXPECTED_POPEN} Popen constructions, saw {spawns}"


def test_every_stage_is_under_the_bar(chain, monkeypatch):
    _spawns, times = _run_chain(chain, monkeypatch)
    assert set(times) == {"bind", "phase1_gates", "phase1_checks", "emit_leg", "terminal_commit"}
    over = {stage: round(t, 3) for stage, t in times.items() if t >= _BAR_S}
    assert not over, f"stage process time over the {_BAR_S}s bar: {over}"


def test_no_git_spawn_inside_a_loop_over_plan_rows():
    """The amplification gate's collector, scoped to ``coordinator_core/ops/plan_chain/`` (its
    own scan roots are ``coordinator_core`` and ``coordinator/bin``, which contain this package;
    it is passed here as the sole root so a finding names only this package)."""
    from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import find_unbatched_per_item_spawns

    sites = find_unbatched_per_item_spawns((_PACKAGE,))
    assert sites == [], [getattr(s, "key", s) for s in sites]
