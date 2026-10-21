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

import json
import subprocess
import sys

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 10
SAMPLES = 5
BUDGET_MS = 500.0

_DRIVER = "coordinator_core.benchmarks.terminal_commit_full_path"


REAL_SHAPE_K = 1
REAL_SHAPE_SAMPLES = 2
REAL_SHAPE_PROCS_CEILING = 4.0


def _sample(workdir, *extra: str, driver: str = _DRIVER, k: int = K_INVOCATIONS) -> dict:
    result = batched_process_time_ms([sys.executable, "-m", driver, str(workdir), *extra], k=k)
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


def _min_sample(
    workdir, *extra: str, driver: str = _DRIVER, k: int = K_INVOCATIONS, samples: int = SAMPLES
) -> dict:
    return min(
        (_sample(workdir, *extra, driver=driver, k=k) for _ in range(samples)),
        key=lambda r: r["process_time_ms"],
    )


def _measure(
    workdir, *extra: str, driver: str = _DRIVER, k: int = K_INVOCATIONS, samples: int = SAMPLES
) -> dict:
    setup = _min_sample(workdir, "--setup-only", *extra, driver=driver, k=k, samples=samples)
    full = _min_sample(workdir, *extra, driver=driver, k=k, samples=samples)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"terminal_commit full path, net tree process time: {net_ms}ms, "
        f"procs_per_call: {net_procs} (setup+call {full['process_time_ms']}ms / "
        f"{full['procs_per_call']} procs; setup-only {setup['process_time_ms']}ms / "
        f"{setup['procs_per_call']} procs; k={k}, min of {samples})"
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


def _build_prebuilt(parent) -> None:
    """One fresh real-shape repo under `parent`, built in its own unmeasured process."""
    parent.mkdir(parents=True)
    built = subprocess.run(
        [sys.executable, "-m", _DRIVER, str(parent), "--kind", "ask", "--real-shape", "--build-only"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert built.returncode == 0, f"real-shape build failed: {built.stderr[-2000:]}"


@pytest.fixture(scope="module")
def real_shape_measurement(tmp_path_factory):
    """Net = full minus the import-only baseline. Every full sample drives its own freshly built
    repo (the call mutates it); the baseline never mutates, so it re-reads the first one."""
    root = tmp_path_factory.mktemp("terminal_commit_budget_real_shape")
    dirs = [root / f"sample{i}" for i in range(REAL_SHAPE_SAMPLES)]
    for d in dirs:
        _build_prebuilt(d)

    def run(d, *extra):
        result = batched_process_time_ms(
            [sys.executable, "-m", _DRIVER, "--prebuilt", str(d), *extra], k=REAL_SHAPE_K
        )
        assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
        return result

    setups = [run(dirs[0], "--setup-only") for _ in range(REAL_SHAPE_SAMPLES)]
    fulls = [run(d) for d in dirs]
    setup = min(setups, key=lambda r: r["process_time_ms"])
    full = min(fulls, key=lambda r: r["process_time_ms"])
    handler_ms = [
        json.loads((d / "handler.json").read_text(encoding="utf-8"))["handler_ms"] for d in dirs
    ]
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    spread = round(
        max(r["process_time_ms"] for r in setups) - min(r["process_time_ms"] for r in setups), 3
    )
    detail = (
        f"terminal_commit full path at real shape, net tree process time: {net_ms}ms, "
        f"procs_per_call: {net_procs} (parent handler ms per sample {handler_ms}; "
        f"setup-only {setup['process_time_ms']}ms spread {spread}ms across {len(setups)} samples; "
        f"full {full['process_time_ms']}ms; k={REAL_SHAPE_K}, min of {REAL_SHAPE_SAMPLES})"
    )
    print(f"TERMINAL-COMMIT-BUDGET {detail}")
    return {
        "net_ms": net_ms,
        "net_procs": net_procs,
        "detail": detail,
        "handler_ms": min(handler_ms),
        "setup_spread_ms": spread,
    }


@pytest.fixture(scope="module")
def halted_record_measurement(tmp_path_factory):
    return _measure(
        tmp_path_factory.mktemp("terminal_commit_budget_halted"),
        driver="coordinator_core.benchmarks.halted_record_commit_full_path",
    )


@pytest.fixture(scope="module")
def halted_incident_measurement(tmp_path_factory):
    return _measure(
        tmp_path_factory.mktemp("terminal_commit_budget_halted_incident"),
        "--incident-shape",
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


def test_terminal_commit_halted_incident_shape_is_under_the_500ms_bar(halted_incident_measurement):
    _assert_under_bar(halted_incident_measurement)
    print(f"TERMINAL-COMMIT-HALTED-INCIDENT {halted_incident_measurement['detail']}")


def test_terminal_commit_grind_mode_is_under_the_500ms_bar(grind_measurement):
    _assert_under_bar(grind_measurement)
    print(f"TERMINAL-COMMIT-GRIND {grind_measurement['detail']}")


def test_terminal_commit_ask_full_path_is_under_the_500ms_bar(ask_full_path_measurement):
    _assert_under_bar(ask_full_path_measurement)
    print(f"TERMINAL-COMMIT-ASK {ask_full_path_measurement['detail']}")


@pytest.mark.designed_red(
    reason="terminal-commit-kill-successor-71f736: red against the killed dispatch.terminal_commit "
    "(attribution 17,195ms net tree); C17 retargets this gate at the successor and removes the marker"
)
def test_terminal_commit_ask_real_shape_is_under_the_500ms_bar(real_shape_measurement):
    """The incident shape: tracked tree, sizing/delivery corpus, stopped-run corpus over a large
    transcript, populated launch dir, a live run token. Red until the kill-bar rebuild lands."""
    _assert_under_bar(real_shape_measurement)
    assert real_shape_measurement["handler_ms"] < BUDGET_MS, (
        f"handler parent process time over the {BUDGET_MS}ms bar: {real_shape_measurement['detail']}"
    )
    assert real_shape_measurement["net_procs"] <= REAL_SHAPE_PROCS_CEILING, (
        f"spawn count over the {REAL_SHAPE_PROCS_CEILING} ceiling: {real_shape_measurement['detail']}"
    )
    print(f"TERMINAL-COMMIT-REAL-SHAPE {real_shape_measurement['detail']}")
