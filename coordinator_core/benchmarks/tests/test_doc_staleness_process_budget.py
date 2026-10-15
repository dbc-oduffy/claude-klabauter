"""Standing gate: the workweek-complete doc-staleness step under the 500ms bar, warm-engine end-to-end.

Two legs share one booted engine and one `LiveTreeAccountant`:
  (ii) graph-bearing: the fixture (`doc_staleness_full_path`) is a real-scale history behind
       README.md and INSTALL.md at the depths docs/research/2026-10-14-doc-staleness-rebuild-spike.md
       § Q5 measured; the report must show its shape and net must be under BUDGET_MS.
  (i)  graph-free: the workweek-brief fixture has no commit-graph; the door must emit the named
       skip and net must be under BUDGET_MS, with a non-zero engine delta so a door that missed
       the engine cannot pass.

The CLI is reached the way it ships: the installed native door, named for the CLI, relays argv to a
warm engine (`invoke.from_argv` with `params.entrypoint`), booted from a hardlink clone of this
tree (`isolated_clone.booted_warm_engine`) so the op under test is the working-tree op.

Basis (requirements ledger R1): per sample, reach_ms is the door client's own tree process time
(`batched_process_time_ms`; its procs_per_call must stay within _REACH_MAX_PROCS, else the door fell back to a cold CLI) and
op_process_ms is the engine's job-object delta (`LiveTreeAccountant`, git children included) across
the same calls. total = reach + op; nothing is subtracted, and the median over SAMPLES samples is
gated. Each sample brackets K_CALLS calls and divides, because job accounting lands on ~15.6ms
ticks. Process time and spawn count only, never wall clock.

THRESHOLD: 500ms is the brightline bar itself (DR-344), not a derived ratchet.

The graph-bearing fixture follows this repo's shape: merges at its ratio, a split commit-graph, no
Bloom filters. Run it explicitly with `-m cadence`.
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
from coordinator_core.benchmarks.doc_staleness_full_path import (
    ENTRYPOINT,
    check_report,
    ensure_fixture,
)
from coordinator_core.benchmarks.isolated_clone import WarmEngineBootTimeout, booted_warm_engine
from coordinator_core.benchmarks.process_time import (
    IS_WINDOWS,
    LiveTreeAccountant,
    batched_process_time_ms,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

K_CALLS = 3
SAMPLES = 5
BUDGET_MS = 500.0
NO_GRAPH_SKIP = "no commit-graph: doc-staleness skipped"

_REACH_MAX_PROCS = 2  # the door client plus the console host Windows allocates for it
_REACH_MAX_MS = 100.0  # a cold CLI fallback pays an interpreter start well above this
_BOOT_WAIT_SECS = 90.0
_CALL_TIMEOUT_S = 120

# Resolved at import: the suite root's conftest quarantines HOME for unmarked tests.
_SETTINGS_HOME = Path(
    os.environ.get("COORDINATOR_SETTINGS_HOME")
    or Path(os.path.expanduser("~")) / ".coordinator-claude-settings"
)
_DOOR_DIR = _SETTINGS_HOME / "bin"
_ENTRY_DOOR = _DOOR_DIR / f"{ENTRYPOINT}.exe"
_PING_DOOR = _DOOR_DIR / "coordinator-invoke.exe"


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def warm_root():
    if not IS_WINDOWS:
        pytest.skip("job-object accounting is Windows-only")
    if not (_ENTRY_DOOR.exists() and _PING_DOOR.exists()):
        pytest.skip(f"{_ENTRY_DOOR} / {_PING_DOOR} not installed on this box")
    try:
        with booted_warm_engine(
            _source_root(),
            prefix="doc-staleness-budget-",
            entrypoint=ENTRYPOINT,
            label="doc_staleness_process_budget",
            boot_wait_secs=_BOOT_WAIT_SECS,
        ) as booted:
            yield booted
    except WarmEngineBootTimeout as exc:
        pytest.skip(str(exc))


def _graph_bearing_fixture(tmp_path_factory) -> Path:
    workdir = tmp_path_factory.mktemp("doc_staleness_budget")
    built = subprocess.run(
        [sys.executable, "-m", "coordinator_core.benchmarks.doc_staleness_full_path", str(workdir), "--build"],
        capture_output=True, text=True, **no_console_creationflags(),
    )
    assert built.returncode == 0, f"fixture build failed: {built.stderr}"
    return ensure_fixture(workdir)


def _graph_free_fixture(tmp_path_factory) -> Path:
    workdir = tmp_path_factory.mktemp("doc_staleness_budget_graph_free")
    built = subprocess.run(
        [sys.executable, "-m", "coordinator_core.benchmarks.workweek_complete_brief_full_path", str(workdir), "--build"],
        capture_output=True, text=True, **no_console_creationflags(),
    )
    assert built.returncode == 0, f"fixture build failed: {built.stderr}"
    return brief_fixture.ensure_fixture(workdir)


def _door_call(cmd: list, env: dict) -> dict:
    served = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=_CALL_TIMEOUT_S,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert served.returncode == 0, f"door call rc={served.returncode}: {served.stderr[:300]!r}"
    return json.loads(served.stdout)


def _sample_leg(acct: LiveTreeAccountant, cmd: list, env: dict, label: str) -> dict:
    samples = []
    for _ in range(SAMPLES):
        before = acct.snapshot()
        reach = batched_process_time_ms(cmd, k=K_CALLS, env=env)
        after = acct.snapshot()
        assert reach["rc"] == 0, f"door call rc={reach['rc']}"
        samples.append({
            "reach_ms": reach["process_time_ms"],
            "reach_procs": reach["procs_per_call"],
            "op_ms": (after["process_time_ms"] - before["process_time_ms"]) / K_CALLS,
            "op_procs": (after["procs"] - before["procs"]) / K_CALLS,
        })
    for s in samples:
        s["total_ms"] = round(s["reach_ms"] + s["op_ms"], 3)
        s["spawns"] = round(s["reach_procs"] + s["op_procs"], 3)
    med = lambda key: round(statistics.median(s[key] for s in samples), 3)  # noqa: E731
    result = {k: med(k) for k in ("reach_ms", "op_ms", "total_ms", "spawns", "reach_procs", "op_procs")}
    result["detail"] = (
        f"doc-staleness step ({label}), warm-engine end-to-end: median total {result['total_ms']}ms = "
        f"reach_ms {result['reach_ms']} + op_process_ms {result['op_ms']}; spawns {result['spawns']} "
        f"(reach {result['reach_procs']} + op {result['op_procs']}); "
        f"samples={SAMPLES} k={K_CALLS}; totals {[s['total_ms'] for s in samples]}"
    )
    result["samples"] = samples
    print(f"DOC-STALENESS-BUDGET {result['detail']}")
    return result


@pytest.fixture(scope="module")
def staleness_measurements(warm_root, tmp_path_factory):
    """Both legs under one accountant: it attaches before any pool worker exists, and a second
    attach after the first leg would miss the workers that leg spawned."""
    _root, server_pid, env = warm_root
    graph_bearing = _graph_bearing_fixture(tmp_path_factory)
    graph_free = _graph_free_fixture(tmp_path_factory)
    bearing_cmd = [str(_ENTRY_DOOR), "--root", str(graph_bearing)]
    free_cmd = [str(_ENTRY_DOOR), "--root", str(graph_free)]
    with LiveTreeAccountant(server_pid) as acct:
        before_probe = acct.snapshot()["procs"]
        probe = subprocess.run(
            [str(_PING_DOOR), "ping", "{}"], capture_output=True, text=True, env=env,
            timeout=_CALL_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert probe.returncode == 0, (
            f"warmth probe failed rc={probe.returncode}: {probe.stderr[:300]!r}"
        )
        assert acct.snapshot()["procs"] > before_probe, (
            "no engine process observed inside the job after the probe: the accountant "
            "attached after the pool spawned, or the probe was not served warm"
        )
        problem = check_report(_door_call(bearing_cmd, env))
        assert problem is None, f"gate would time a wrong result: {problem}"
        free_report = _door_call(free_cmd, env)
        bearing = _sample_leg(acct, bearing_cmd, env, "graph-bearing")
        free = _sample_leg(acct, free_cmd, env, "graph-free")
    return {"bearing": bearing, "free": free, "free_report": free_report}


def test_door_reach_is_warm(staleness_measurements):
    m = staleness_measurements["bearing"]
    assert all(s["reach_procs"] <= _REACH_MAX_PROCS and s["reach_ms"] < _REACH_MAX_MS for s in m["samples"]), (
        f"the door did more than relay (cold CLI fallback): {m['detail']}"
    )
    assert m["op_procs"] >= 1, f"no git child charged to the engine: {m['detail']}"


def test_doc_staleness_is_under_the_500ms_bar(staleness_measurements):
    m = staleness_measurements["bearing"]
    assert m["op_ms"] > 0 and m["op_procs"] > 0, f"op cost not observed in the engine: {m['detail']}"
    assert m["total_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"


def test_graph_free_emits_the_named_skip_under_the_500ms_bar(staleness_measurements):
    m = staleness_measurements["free"]
    report = staleness_measurements["free_report"]
    assert report.get("skipped") == NO_GRAPH_SKIP, f"no named skip on a graph-free repo: {report!r}"
    assert m["op_ms"] > 0, f"op cost not observed in the engine: {m['detail']}"
    assert m["total_ms"] < BUDGET_MS, f"over the {BUDGET_MS}ms bar: {m['detail']}"
