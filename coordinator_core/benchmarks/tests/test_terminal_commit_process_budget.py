"""Standing gate: dispatch.terminal_commit's full path under the 500ms bar, by tree process time.

The full path is the product commit, the review-stamp mint, the coded-stamp
commit and the implemented flip. `test_terminal_commit.py` used to bound it
with parent-only `time.process_time()`, which cannot see the git children (266ms
parent against 556ms tree at baseline); this gate measures the tree.

UNIT: process time and spawn count from `batched_process_time_ms`
(k=K_INVOCATIONS, job-object / kqueue / fork accounting), never wall clock
(CLAUDE.md, The brightline). Net = (setup + call) minus the setup-only run of
the same driver (`terminal_commit_full_path --setup-only`), each taken as the
min over SAMPLES samples so peer load inflates neither term.

THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.
Process counts are reported beside it and not asserted on; on Windows each git
spawn is two processes (child plus console host).

HOOKS: the fixture installs no hook. DR-356 rules the commit hook chain out of
an op's own budget; a repo carrying hooks would read provisional here and the
hook figure belongs beside this one, never folded into it.
"""

from __future__ import annotations

import sys

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 10
SAMPLES = 5
BUDGET_MS = 500.0

_DRIVER = "coordinator_core.benchmarks.terminal_commit_full_path"


def _sample(workdir, *extra: str, driver: str = _DRIVER) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", driver, str(workdir), *extra], k=K_INVOCATIONS
    )
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


def _min_sample(workdir, *extra: str, driver: str = _DRIVER) -> dict:
    return min(
        (_sample(workdir, *extra, driver=driver) for _ in range(SAMPLES)), key=lambda r: r["process_time_ms"]
    )


def _measure(workdir, *extra: str, driver: str = _DRIVER) -> dict:
    setup = _min_sample(workdir, "--setup-only", *extra, driver=driver)
    full = _min_sample(workdir, *extra, driver=driver)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"terminal_commit full path, net tree process time: {net_ms}ms, "
        f"procs_per_call: {net_procs} (setup+call {full['process_time_ms']}ms / "
        f"{full['procs_per_call']} procs; setup-only {setup['process_time_ms']}ms / "
        f"{setup['procs_per_call']} procs; k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"TERMINAL-COMMIT-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


@pytest.fixture(scope="module")
def full_path_measurement(tmp_path_factory):
    return _measure(tmp_path_factory.mktemp("terminal_commit_budget"))


@pytest.fixture(scope="module")
def mise_full_path_measurement(tmp_path_factory):
    return _measure(tmp_path_factory.mktemp("terminal_commit_budget_mise"), "--kind", "mise")


@pytest.fixture(scope="module")
def ask_full_path_measurement(tmp_path_factory):
    return _measure(tmp_path_factory.mktemp("terminal_commit_budget_ask"), "--kind", "ask")


@pytest.fixture(scope="module")
def halted_record_measurement(tmp_path_factory):
    return _measure(
        tmp_path_factory.mktemp("terminal_commit_budget_halted"),
        driver="coordinator_core.benchmarks.halted_record_commit_full_path",
    )


@pytest.fixture(scope="module")
def grind_measurement(tmp_path_factory):
    return _measure(
        tmp_path_factory.mktemp("terminal_commit_budget_grind"),
        driver="coordinator_core.benchmarks.grind_terminal_commit_full_path",
    )


def _assert_under_bar(m: dict) -> None:
    # Positive net ms is necessary, not sufficient: completion is the driver's rc==0
    # (last of k runs only, see batched_process_time_ms). Zero net procs is the goal
    # (in-process snapshot), so procs prove nothing about observation.
    assert m["net_ms"] > 0 and m["net_procs"] >= 0, f"call cost not observed (full <= setup-only): {m['detail']}"
    assert m["net_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"


def test_terminal_commit_full_path_is_under_the_500ms_bar(full_path_measurement):
    _assert_under_bar(full_path_measurement)


def test_terminal_commit_mise_full_path_is_under_the_500ms_bar(mise_full_path_measurement):
    _assert_under_bar(mise_full_path_measurement)


def test_terminal_commit_halted_record_is_under_the_500ms_bar(halted_record_measurement):
    _assert_under_bar(halted_record_measurement)
    print(f"TERMINAL-COMMIT-HALTED {halted_record_measurement['detail']}")


def test_terminal_commit_grind_mode_is_under_the_500ms_bar(grind_measurement):
    _assert_under_bar(grind_measurement)
    print(f"TERMINAL-COMMIT-GRIND {grind_measurement['detail']}")


def test_terminal_commit_ask_full_path_cost_is_observed(ask_full_path_measurement):
    m = ask_full_path_measurement
    assert m["net_ms"] > 0 and m["net_procs"] >= 0, f"call cost not observed (full <= setup-only): {m['detail']}"
    print(f"TERMINAL-COMMIT-ASK {m['detail']}")
