"""Falsifier: on a stamped engine copy with no warm server, each assembler brief CLI serves
in one interpreter under its high-water; an unstamped copy still spawns a child and refuses.

Measures process time and spawn count only (never wall clock). Run serially:
    python -m pytest coordinator_core/tests/test_stamped_cold_cli_floor.py -m cadence -p no:randomly
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import (
    IS_WINDOWS,
    single_invocation_tree_process_time,
)
from coordinator_core.tests._stamped_engine_fixture import (
    build_engine_copy,
    cold_cli_env,
    spawn_path_env,
)
from coordinator_core.tests.test_assembler_cli_boundary_budget import (
    GATE_HARD_CEILING_MS,
    GATE_SAMPLES_PER_BRIEF,
    GATE_TOTAL_PROCESS_TIME_CEILING_MS,
    GATE_TOTAL_PROCS_CEILING,
    HIGH_WATER_PICKUP_MS,
    CHILD_PROCS,
    _seed_baton_repo,
    _seed_pickup_repo,
    _seed_wsc_repo,
    _skip_reason_for_platform,
)

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_UNSTAMPED_PROCS = 4 if IS_WINDOWS else 2
_BRIEF_COUNT = 3

_skip = _skip_reason_for_platform()
if _skip is not None:
    pytestmark.append(pytest.mark.skip(reason=_skip))


@pytest.fixture(scope="module")
def stamped_engine(tmp_path_factory) -> Path:
    return build_engine_copy(tmp_path_factory.mktemp("stamped-engine"), stamped=True)


@pytest.fixture(scope="module")
def unstamped_engine(tmp_path_factory) -> Path:
    return build_engine_copy(tmp_path_factory.mktemp("unstamped-engine"), stamped=False)


def _measure(cmd, repo, engine, out_dir: Path, tag: str, *, spawn_path: bool = False) -> dict:
    out = out_dir / f"{tag}.out"
    err = out_dir / f"{tag}.err"
    result = single_invocation_tree_process_time(
        cmd,
        cwd=str(repo),
        env=spawn_path_env(cold_cli_env(engine)) if spawn_path else cold_cli_env(engine),
        stdout_path=str(out),
        stderr_path=str(err),
    )
    result["stdout"] = out.read_text(encoding="utf-8", errors="replace")
    result["stderr"] = err.read_text(encoding="utf-8", errors="replace")
    return result


_BRIEFS = [
    ("pickup", _seed_pickup_repo, HIGH_WATER_PICKUP_MS),
    ("baton", _seed_baton_repo, None),
    ("wsc", _seed_wsc_repo, None),
]


@pytest.mark.parametrize("name,seed,high_water", _BRIEFS, ids=[b[0] for b in _BRIEFS])
def test_stamped_brief_serves_in_one_interpreter(name, seed, high_water, stamped_engine, tmp_path):
    repo, cmd = seed(tmp_path, stamped_engine / "coordinator" / "bin")
    spawn = _measure(cmd, repo, stamped_engine, tmp_path, f"{name}-spawn", spawn_path=True)
    assert spawn["rc"] == 0, f"{name} spawn path: rc={spawn['rc']} stderr={spawn['stderr'][:500]}"
    total_ms = 0.0
    total_procs = 0
    for i in range(GATE_SAMPLES_PER_BRIEF):
        r = _measure(cmd, repo, stamped_engine, tmp_path, f"{name}-{i}")
        assert r["rc"] == 0, f"{name}: rc={r['rc']} stderr={r['stderr'][:500]}"
        json.loads(r["stdout"])
        assert r["procs"] == spawn["procs"] - CHILD_PROCS, (
            f"{name}: in-process procs {r['procs']} != spawn-path procs {spawn['procs']} "
            f"minus the bootstrap child's {CHILD_PROCS}"
        )
        if name == "pickup":
            assert r["procs"] == (2 if IS_WINDOWS else 1)
        assert r["process_time_ms"] <= GATE_HARD_CEILING_MS, (
            f"{name}: {r['process_time_ms']}ms exceeds the {GATE_HARD_CEILING_MS}ms brightline"
        )
        assert high_water is None or r["process_time_ms"] <= high_water, (
            f"{name}: {r['process_time_ms']}ms exceeds high-water {high_water}ms"
        )
        total_ms += r["process_time_ms"]
        total_procs += r["procs"]
    assert total_ms <= GATE_TOTAL_PROCESS_TIME_CEILING_MS / _BRIEF_COUNT
    assert total_procs <= GATE_TOTAL_PROCS_CEILING / _BRIEF_COUNT


def test_unstamped_copy_still_refuses_via_child(unstamped_engine, tmp_path):
    repo, cmd = _seed_pickup_repo(tmp_path, unstamped_engine / "coordinator" / "bin")
    r = _measure(cmd, repo, unstamped_engine, tmp_path, "unstamped")
    assert r["rc"] != 0
    assert "has no build stamp" in r["stderr"]
    assert r["procs"] == _UNSTAMPED_PROCS, (
        f"unstamped: tree spawned {r['procs']} processes, expected {_UNSTAMPED_PROCS}"
    )
    assert r["process_time_ms"] <= GATE_HARD_CEILING_MS
