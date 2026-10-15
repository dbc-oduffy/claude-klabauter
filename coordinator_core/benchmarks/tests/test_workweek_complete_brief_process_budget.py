"""Standing gate: the workweek-complete brief under the 500ms bar, by tree process time.

The fixture seeds a real-scale history (HISTORY_COMMITS commits), the doc registry, the
pending-release completion corpus and the register-bearing sizings, because the brief's cost is
history-depth- and corpus-sensitive work a one-commit repo cannot show. Same method as
`test_quick_wrap_brief_process_budget.py`: net = (setup + call) minus the setup-only run of the
same driver, each the min over SAMPLES samples of K_INVOCATIONS runs, sampled alternately. The
fixture is built once before sampling, so setup-only is interpreter start plus driver imports.

UNIT: process time and spawn count from `batched_process_time_ms`, never wall clock (CLAUDE.md,
The brightline). THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.

A second leg reaches the brief the way the ceremony does: the installed native door relays to a
warm engine booted from a hardlink clone of this tree. Its net is the door client's tree process
time plus the engine's `LiveTreeAccountant` job-object delta, with a non-zero engine delta so a door
that missed the engine cannot pass.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks import workweek_complete_brief_full_path as brief_fixture
from coordinator_core.benchmarks.isolated_clone import WarmEngineBootTimeout, booted_warm_engine
from coordinator_core.benchmarks.process_time import (
    IS_WINDOWS,
    LiveTreeAccountant,
    batched_process_time_ms,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_INVOCATIONS = 2
SAMPLES = 5
BUDGET_MS = 500.0

_DRIVER = "coordinator_core.benchmarks.workweek_complete_brief_full_path"
_ENTRYPOINT = "workweek-complete-brief"
_BOOT_WAIT_SECS = 90.0
_CALL_TIMEOUT_S = 120

# Resolved at import: the suite root's conftest quarantines HOME for unmarked tests.
_SETTINGS_HOME = Path(
    os.environ.get("COORDINATOR_SETTINGS_HOME")
    or Path(os.path.expanduser("~")) / ".coordinator-claude-settings"
)
_ENTRY_DOOR = _SETTINGS_HOME / "bin" / f"{_ENTRYPOINT}.exe"
_PING_DOOR = _SETTINGS_HOME / "bin" / "coordinator-invoke.exe"


def _sample(workdir, *extra: str) -> dict:
    result = batched_process_time_ms(
        [sys.executable, "-m", _DRIVER, str(workdir), *extra], k=K_INVOCATIONS
    )
    assert result["rc"] == 0, f"driver exited {result['rc']}: {result!r}"
    return result


def _build(workdir) -> None:
    built = subprocess.run(
        [sys.executable, "-m", _DRIVER, str(workdir), "--build"],
        capture_output=True, text=True, **no_console_creationflags(),
    )
    assert built.returncode == 0, f"fixture build failed: {built.stderr}"


def _interleaved_mins(workdir) -> tuple[dict, dict]:
    """(setup-only, setup+call), each the min of SAMPLES samples taken alternately."""
    _build(workdir)
    setups, fulls = [], []
    for _ in range(SAMPLES):
        setups.append(_sample(workdir, "--setup-only"))
        fulls.append(_sample(workdir))

    def _least(rs):
        return min(rs, key=lambda r: r["process_time_ms"])

    return _least(setups), _least(fulls)


@pytest.fixture(scope="module")
def brief_measurement(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("workweek_complete_brief_budget")
    setup, full = _interleaved_mins(workdir)
    net_ms = round(full["process_time_ms"] - setup["process_time_ms"], 3)
    net_procs = round(full["procs_per_call"] - setup["procs_per_call"], 3)
    detail = (
        f"workweek-complete brief, net tree process time: {net_ms}ms, procs_per_call: {net_procs} "
        f"(setup+call {full['process_time_ms']}ms / {full['procs_per_call']} procs; "
        f"setup-only {setup['process_time_ms']}ms / {setup['procs_per_call']} procs; "
        f"k={K_INVOCATIONS}, min of {SAMPLES})"
    )
    print(f"WORKWEEK-COMPLETE-BRIEF-BUDGET {detail}")
    return {"net_ms": net_ms, "net_procs": net_procs, "detail": detail}


def test_workweek_complete_brief_is_under_the_500ms_bar(brief_measurement):
    m = brief_measurement
    assert m["net_ms"] > 0, (
        f"brief cost not observed (full <= setup-only): {m['detail']}"
    )
    assert m["net_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"


@pytest.fixture(scope="module")
def warm_door_measurement(tmp_path_factory):
    """The brief reached the way the ceremony reaches it: the installed native door relays to a
    warm engine booted from a hardlink clone of this tree, cwd = the gate fixture. Net = the door
    client's tree process time plus the engine's `LiveTreeAccountant` job-object delta."""
    if not IS_WINDOWS:
        pytest.skip("job-object accounting is Windows-only")
    if not (_ENTRY_DOOR.exists() and _PING_DOOR.exists()):
        pytest.skip(f"{_ENTRY_DOOR} / {_PING_DOOR} not installed on this box")
    workdir = tmp_path_factory.mktemp("workweek_complete_brief_warm_door")
    _build(workdir)
    fixture = brief_fixture.ensure_fixture(workdir)
    try:
        with booted_warm_engine(
            Path(__file__).resolve().parents[3],
            prefix="workweek-brief-budget-",
            entrypoint=_ENTRYPOINT,
            label="workweek_complete_brief_process_budget",
            boot_wait_secs=_BOOT_WAIT_SECS,
        ) as (_root, server_pid, env):
            with LiveTreeAccountant(server_pid) as acct:
                before_probe = acct.snapshot()["procs"]
                probe = subprocess.run(
                    [str(_PING_DOOR), "ping", "{}"], capture_output=True, text=True, env=env,
                    cwd=str(fixture), timeout=_CALL_TIMEOUT_S, **no_console_creationflags(),
                )
                assert probe.returncode == 0, f"warmth probe rc={probe.returncode}: {probe.stderr[:300]!r}"
                assert acct.snapshot()["procs"] > before_probe, (
                    "no engine process observed inside the job after the probe: the accountant "
                    "attached after the pool spawned, or the probe was not served warm"
                )
                cmd = [str(_ENTRY_DOOR)]
                served = subprocess.run(
                    cmd, capture_output=True, text=True, env=env, cwd=str(fixture),
                    timeout=_CALL_TIMEOUT_S, **no_console_creationflags(),
                )
                assert served.returncode == 0, f"door rc={served.returncode}: {served.stderr[:300]!r}"
                ids = [p.get("id", "") for p in json.loads(served.stdout).get("judgment_points", [])]
                assert brief_fixture.SENTINEL_ID in ids, f"warm door timed a wrong result: {ids[:5]!r}"
                samples = []
                for _ in range(SAMPLES):
                    before = acct.snapshot()
                    reach = batched_process_time_ms(cmd, k=K_INVOCATIONS, env=env, cwd=str(fixture))
                    after = acct.snapshot()
                    assert reach["rc"] == 0, f"door call rc={reach['rc']}"
                    op_ms = (after["process_time_ms"] - before["process_time_ms"]) / K_INVOCATIONS
                    op_procs = (after["procs"] - before["procs"]) / K_INVOCATIONS
                    samples.append({
                        "reach_ms": reach["process_time_ms"], "op_ms": op_ms,
                        "total_ms": round(reach["process_time_ms"] + op_ms, 3),
                        "spawns": round(reach["procs_per_call"] + op_procs, 3),
                    })
    except WarmEngineBootTimeout as exc:
        pytest.skip(str(exc))
    med = lambda key: round(statistics.median(s[key] for s in samples), 3)  # noqa: E731
    detail = (
        f"workweek-complete brief, warm door end-to-end: median total {med('total_ms')}ms = "
        f"reach_ms {med('reach_ms')} + op_process_ms {med('op_ms')}; spawns {med('spawns')}; "
        f"samples={SAMPLES} k={K_INVOCATIONS}; totals {[s['total_ms'] for s in samples]}"
    )
    print(f"WORKWEEK-COMPLETE-BRIEF-WARM-DOOR {detail}")
    return {"total_ms": med("total_ms"), "op_ms": med("op_ms"), "detail": detail}


def test_workweek_complete_brief_warm_door_is_under_the_500ms_bar(warm_door_measurement):
    m = warm_door_measurement
    assert m["op_ms"] > 0, f"op cost not observed in the engine: {m['detail']}"
    assert m["total_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"
