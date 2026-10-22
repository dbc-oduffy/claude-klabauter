"""Standing gate: dispatch.land_run's full path under the 500ms bar, by tree process time and spawns.

The landing is one scoped commit; the review stamp, coded stamp, implemented flip and the rest of
the close run in the close job, which the drivers point at a no-op script (the job's own work is
gated by `test_close_background_process_budget.py`). Shapes: plan, mise, ask, ask at real shape,
reclose (a landed run called again with a fresh criterion), halted record, halted incident, halted
refire (#60), grind and fanout. The fanout shape is driven through `land_run._handler` on the
real-shape fixture with the `ask_fanout` relay stubbed.

UNIT: process time and spawn count from `batched_process_time_ms`
(k=K_INVOCATIONS, job-object / kqueue / fork accounting), never wall clock
(CLAUDE.md, The brightline). Net = (setup + call) minus the setup-only run of
the same driver (`terminal_commit_full_path --setup-only`), each taken as the
min over SAMPLES samples so peer load inflates neither term.

THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.
Process counts are asserted per shape against the ceilings below (DR-368: a target is a number);
on Windows a git spawn counts as two processes (child plus console host).

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
_HALTED_DRIVER = "coordinator_core.benchmarks.halted_record_commit_full_path"
_GRIND_DRIVER = "coordinator_core.benchmarks.grind_terminal_commit_full_path"

# Net procs_per_call ceilings, from docs/research/spike-verdicts/2026-10-21-land-run-minimal-shape.md.
# Each spawn a shape may make, and its use:
#   git hash-object --stdin-paths  one call, only when a path is filter- or attribute-pinned and
#                                  `commit_paths` cannot hash its bytes in process (typical: none).
#   detached close job launch      one `spawn_detached` of `close.background`, so the review stamp,
#                                  coded stamp, implemented flip, sizing ship, cascade and close
#                                  commit leave the landing path.
# product landings (plan, mise, ask, ask real shape): hash-object fallback + close launch.
# Ceilings are the worst-case allowance; the fixtures pin no path, so the typical net is 0 git spawns.
# Trap: a regression adding spawns up to a ceiling still passes; tighten to measured nets.
PROCS_CEILING_PLAN = 2.0
PROCS_CEILING_MISE = 2.0
PROCS_CEILING_ASK = 2.0
PROCS_CEILING_ASK_REAL_SHAPE = 2.0
# reclose: a landed run called again with a fresh criterion. No commit and no hash-object fallback;
# the one spawn is the re-queued close job's detached launch (`spawn_detached` of `close.background`),
# so the ceiling is the product shape's and the typical net is that single launch.
PROCS_CEILING_RECLOSE = PROCS_CEILING_PLAN
# halted record and incident: hash-object fallback only; a halt launches no close job.
PROCS_CEILING_HALTED = 1.0
PROCS_CEILING_HALTED_INCIDENT = 1.0
# halted refire (#60): a product landing, so hash-object fallback + close launch.
PROCS_CEILING_HALTED_REFIRE = 2.0
# grind: hash-object fallback only; no close job.
PROCS_CEILING_GRIND = 1.0
# fanout record: hash-object fallback only; the `ask_fanout` relay is the next op, not the landing.
PROCS_CEILING_FANOUT = 1.0

REAL_SHAPE_K = 1
REAL_SHAPE_SAMPLES = 2


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
        f"land_run full path, net tree process time: {net_ms}ms, "
        f"procs_per_call: {net_procs} (setup+call {full['process_time_ms']}ms / "
        f"{full['procs_per_call']} procs; setup-only {setup['process_time_ms']}ms / "
        f"{setup['procs_per_call']} procs; k={k}, min of {samples})"
    )
    print(f"LAND-RUN-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


@pytest.fixture(scope="module")
def full_path_measurement(tmp_path_factory):
    return _measure(tmp_path_factory.mktemp("land_run_budget"))


@pytest.fixture(scope="module")
def mise_full_path_measurement(tmp_path_factory):
    return _measure(tmp_path_factory.mktemp("land_run_budget_mise"), "--kind", "mise")


@pytest.fixture(scope="module")
def ask_full_path_measurement(tmp_path_factory):
    return _measure(tmp_path_factory.mktemp("land_run_budget_ask"), "--kind", "ask")


@pytest.fixture(scope="module")
def reclose_measurement(tmp_path_factory):
    """Trap: the landing the reclose re-closes is paid in `--build-only`, never in a measured
    process; its fixture git and detached launch spawn conhost.exe children Windows counts
    nondeterministically, which read as a 2.1 net against the 2.0 ceiling."""
    root = tmp_path_factory.mktemp("land_run_budget_reclose")
    dirs = [root / f"sample{i}" for i in range(SAMPLES)]
    for d in dirs:
        _build_prebuilt(d, "reclose", real_shape=False)
    setup = _min_prebuilt(dirs[0], "--setup-only")
    full = min((_sample_prebuilt(d) for d in dirs), key=lambda r: r["process_time_ms"])
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"land_run reclose on a prebuilt landing, net tree process time: {net_ms}ms, "
        f"procs_per_call: {net_procs} (full {full['process_time_ms']}ms / {full['procs_per_call']} procs; "
        f"setup-only {setup['process_time_ms']}ms / {setup['procs_per_call']} procs; "
        f"k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"LAND-RUN-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


def _sample_prebuilt(d, *extra: str) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", _DRIVER, "--prebuilt", str(d), *extra], k=K_INVOCATIONS
    )
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


def _min_prebuilt(d, *extra: str) -> dict:
    return min((_sample_prebuilt(d, *extra) for _ in range(SAMPLES)), key=lambda r: r["process_time_ms"])


def _build_prebuilt(parent, kind: str, real_shape: bool = True) -> None:
    """One fresh repo under `parent`, built in its own unmeasured process."""
    parent.mkdir(parents=True)
    shape = ["--real-shape"] if real_shape else []
    built = subprocess.run(
        [sys.executable, "-m", _DRIVER, str(parent), "--kind", kind, *shape, "--build-only"],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert built.returncode == 0, f"real-shape build failed: {built.stderr[-2000:]}"


def _real_shape_measure(root, kind: str) -> dict:
    """Net = full minus the import-only baseline. Every full sample drives its own freshly built
    repo (the call mutates it); the baseline never mutates, so it re-reads the first one."""
    dirs = [root / f"sample{i}" for i in range(REAL_SHAPE_SAMPLES)]
    for d in dirs:
        _build_prebuilt(d, kind)

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
        f"land_run {kind} at real shape, net tree process time: {net_ms}ms, "
        f"procs_per_call: {net_procs} (parent handler ms per sample {handler_ms}; "
        f"setup-only {setup['process_time_ms']}ms spread {spread}ms across {len(setups)} samples; "
        f"full {full['process_time_ms']}ms; k={REAL_SHAPE_K}, min of {REAL_SHAPE_SAMPLES})"
    )
    print(f"LAND-RUN-BUDGET {detail}")
    return {
        "net_ms": net_ms,
        "net_procs": net_procs,
        "detail": detail,
        "handler_ms": min(handler_ms),
        "setup_spread_ms": spread,
    }


@pytest.fixture(scope="module")
def real_shape_measurement(tmp_path_factory):
    return _real_shape_measure(tmp_path_factory.mktemp("land_run_budget_real_shape"), "ask")


@pytest.fixture(scope="module")
def fanout_measurement(tmp_path_factory):
    return _real_shape_measure(tmp_path_factory.mktemp("land_run_budget_fanout"), "fanout")


@pytest.fixture(scope="module")
def halted_record_measurement(tmp_path_factory):
    return _measure(tmp_path_factory.mktemp("land_run_budget_halted"), driver=_HALTED_DRIVER)


@pytest.fixture(scope="module")
def halted_incident_measurement(tmp_path_factory):
    return _measure(
        tmp_path_factory.mktemp("land_run_budget_halted_incident"), "--incident-shape", driver=_HALTED_DRIVER
    )


@pytest.fixture(scope="module")
def halted_refire_measurement(tmp_path_factory):
    return _measure(
        tmp_path_factory.mktemp("land_run_budget_halted_refire"),
        "--refire-withheld",
        driver=_HALTED_DRIVER,
        k=3,
        samples=3,
    )


@pytest.fixture(scope="module")
def grind_measurement(tmp_path_factory):
    return _measure(tmp_path_factory.mktemp("land_run_budget_grind"), driver=_GRIND_DRIVER)


def _assert_under_bar(m: dict, procs_ceiling: float) -> None:
    # Positive net ms is necessary, not sufficient: completion is the driver's rc==0
    # (last of k runs only, see batched_process_time_ms).
    assert m["net_ms"] > 0 and m["net_procs"] >= 0, f"call cost not observed (full <= setup-only): {m['detail']}"
    assert m["net_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"
    assert m["net_procs"] <= procs_ceiling, f"spawn count over the {procs_ceiling} ceiling: {m['detail']}"


def test_land_run_full_path_is_under_the_500ms_bar(full_path_measurement):
    _assert_under_bar(full_path_measurement, PROCS_CEILING_PLAN)


def test_land_run_reclose_is_under_the_500ms_bar(reclose_measurement):
    _assert_under_bar(reclose_measurement, PROCS_CEILING_RECLOSE)
    print(f"LAND-RUN-RECLOSE {reclose_measurement['detail']}")


def test_land_run_mise_full_path_is_under_the_500ms_bar(mise_full_path_measurement):
    _assert_under_bar(mise_full_path_measurement, PROCS_CEILING_MISE)


def test_land_run_halted_record_is_under_the_500ms_bar(halted_record_measurement):
    _assert_under_bar(halted_record_measurement, PROCS_CEILING_HALTED)
    print(f"LAND-RUN-HALTED {halted_record_measurement['detail']}")


def test_land_run_halted_incident_shape_is_under_the_500ms_bar(halted_incident_measurement):
    _assert_under_bar(halted_incident_measurement, PROCS_CEILING_HALTED_INCIDENT)
    print(f"LAND-RUN-HALTED-INCIDENT {halted_incident_measurement['detail']}")


def test_land_run_halted_refire_lands_withheld_product_under_the_500ms_bar(halted_refire_measurement):
    _assert_under_bar(halted_refire_measurement, PROCS_CEILING_HALTED_REFIRE)
    print(f"LAND-RUN-HALTED-REFIRE {halted_refire_measurement['detail']}")


def test_land_run_grind_mode_is_under_the_500ms_bar(grind_measurement):
    _assert_under_bar(grind_measurement, PROCS_CEILING_GRIND)
    print(f"LAND-RUN-GRIND {grind_measurement['detail']}")


def test_land_run_ask_full_path_is_under_the_500ms_bar(ask_full_path_measurement):
    _assert_under_bar(ask_full_path_measurement, PROCS_CEILING_ASK)
    print(f"LAND-RUN-ASK {ask_full_path_measurement['detail']}")


def test_land_run_ask_real_shape_is_under_the_500ms_bar(real_shape_measurement):
    """The incident shape: tracked tree, sizing/delivery corpus, stopped-run corpus over a large
    transcript, populated launch dir, a live run token."""
    _assert_under_bar(real_shape_measurement, PROCS_CEILING_ASK_REAL_SHAPE)
    assert real_shape_measurement["handler_ms"] < BUDGET_MS, (
        f"handler parent process time over the {BUDGET_MS}ms bar: {real_shape_measurement['detail']}"
    )
    print(f"LAND-RUN-REAL-SHAPE {real_shape_measurement['detail']}")


def test_land_run_fanout_at_real_shape_is_under_the_500ms_bar(fanout_measurement):
    _assert_under_bar(fanout_measurement, PROCS_CEILING_FANOUT)
    assert fanout_measurement["handler_ms"] < BUDGET_MS, (
        f"handler parent process time over the {BUDGET_MS}ms bar: {fanout_measurement['detail']}"
    )
    print(f"LAND-RUN-FANOUT {fanout_measurement['detail']}")
