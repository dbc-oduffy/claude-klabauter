"""A hook-mode Bash call through the door answers within the 500ms bar, and the bound's two edges hold.

The live test drives the benchmark's `measure()` against the installed door; the unit tests pin the report
shape and the fail-open classifier without a door; the throwaway-engine legs build the door from this tree and
pin both sides of the read bound: a guard that answers is returned, a dead engine fails open fast.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks import door_hook_read_latency as bench
from coordinator_core.win_portability import no_console_creationflags

_REPORT_KEYS = {"runs", "max_process_ms", "median_process_ms", "fail_open", "door"}


def test_summarize_reports_the_json_shape_and_max():
    report = bench.summarize([3.0, 7.04, 5.0], 1, "/d")
    assert set(report) == _REPORT_KEYS
    assert report == {"runs": 3, "max_process_ms": 7.0, "median_process_ms": 5.0, "fail_open": 1, "door": "/d"}


def test_fail_open_is_the_engine_failure_allow_envelope_only():
    assert bench.is_fail_open('{"systemMessage": "x: guard not evaluated, command allowed."}')
    assert bench.is_fail_open('{"systemMessage":"coordinator: guard did not run (engine unreachable)"}')
    assert bench.is_fail_open("")
    assert not bench.is_fail_open('{"jsonrpc": "2.0", "id": 1, "result": {}}')
    assert not bench.is_fail_open('{"hookSpecificOutput":{"permissionDecision":"allow"}}')


def test_the_commit_leg_payload_is_commit_shaped_and_the_benign_one_is_not():
    commit = json.loads(bench.hook_payload(Path("."), bench.COMMIT_COMMAND))["tool_input"]["command"]
    benign = json.loads(bench.hook_payload(Path(".")))["tool_input"]["command"]
    assert commit.startswith("git commit") and not benign.startswith("git commit")


@pytest.mark.pending_fix
@pytest.mark.spawns_process
@pytest.mark.warm_tier
@pytest.mark.skipif(os.name == "nt", reason="drives the POSIX door")
def test_an_installed_door_hook_read_answers_under_the_bar_without_failing_open():
    door = bench.installed_door()
    if door is None:
        pytest.skip("no installed coordinator-invoke door")
    report = bench.measure(runs=bench.DEFAULT_RUNS, door=door)
    assert set(report) == _REPORT_KEYS
    assert report["runs"] == bench.DEFAULT_RUNS
    assert report["fail_open"] == 0, report
    assert report["max_process_ms"] <= bench.BAR_MS, report


@pytest.fixture(scope="module")
def built_door(tmp_path_factory) -> Path:
    """The door built from the working-tree source, so the legs below measure the bound in this tree."""
    compiler = next((shutil.which(c) for c in ("cc", "clang", "gcc") if shutil.which(c)), None)
    if compiler is None or os.name == "nt":
        pytest.skip("no C compiler or not POSIX")
    door_dir = Path(__file__).resolve().parents[1]
    out = tmp_path_factory.mktemp("bench-door") / "coordinator-invoke"
    subprocess.run(
        [compiler, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', '-DBUILD_ENGINE_ROOT=""', "-o", str(out),
         str(door_dir / "door_posix.c"), str(door_dir / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@pytest.mark.skipif(os.name == "nt", reason="drives the POSIX door")
def test_a_guard_answering_inside_the_bound_returns_its_verdict_and_a_dead_engine_fails_open_fast(built_door):
    slow = bench.measure_slow_guard(150, runs=2, door=built_door)
    assert slow["fail_open"] == 0, slow
    assert slow["max_elapsed_ms"] >= 150, "the delay did not reach the guard"
    dead = bench.measure_dead_engine(runs=2, door=built_door)
    assert dead["fail_open"] == 2, dead
    assert dead["max_elapsed_ms"] < bench.BAR_MS, dead


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@pytest.mark.skipif(os.name == "nt", reason="drives the POSIX door")
def test_a_guard_answering_at_900ms_is_not_failed_open_by_the_500ms_bound(built_door):
    report = bench.measure_slow_guard(900, runs=2, door=built_door)
    assert report["fail_open"] == 0, report
    assert report["max_process_ms"] <= bench.BAR_MS, report
