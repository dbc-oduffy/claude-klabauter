"""Cost gate for guard-heavy-command-admission: zero process spawns per call, light-call process
time under the 50ms bar, and the heavy-call census cost measured and reported.

time.process_time ticks at 15.6ms on Windows, so every figure is a mean over a batch; wall clock
is never asserted.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from coordinator_core.bash_guards import guard_heavy_command_admission as guard
from coordinator_core.bash_guards._heavy_admission_contract import MemoryReading

LIGHT_BATCH = 200
CENSUS_BATCH = 20
LIGHT_BAR_MS = 50
CENSUS_BAR_MS = 500

LIGHT_COMMANDS = (
    "git status",
    "ls -la",
    "grep tsc README.md",
    'echo "cargo build"',
    "git log -- build",
    "cat pyproject.toml",
    "python -c \"print('hello')\"",
    "Get-ChildItem",
)

HEAVY_COMMANDS = (
    "tsc --noEmit",
    "pnpm run build",
    "cargo build",
    "python -m pytest",
    "pwsh -Command RunUAT.bat BuildCookRun",
    "python project_rag_cli.py reindex",
)


def _spawn_trap(*_a, **_k):
    raise AssertionError("guard check spawned a process")


@pytest.fixture()
def no_spawn(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _spawn_trap)
    monkeypatch.setattr(os, "system", _spawn_trap)
    monkeypatch.setattr(os, "popen", _spawn_trap)
    for name in ("spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp",
                 "spawnvpe", "posix_spawn", "posix_spawnp"):
        monkeypatch.setattr(os, name, _spawn_trap, raising=False)


def _payload(command):
    return {
        "tool_name": "Bash",
        "session_id": "cost-gate",
        "tool_input": {"command": command},
    }


def test_light_calls_spawn_nothing_and_stay_under_the_bar(no_spawn):
    for command in LIGHT_COMMANDS:
        assert guard.check(_payload(command)) is None
    start = time.process_time()
    for i in range(LIGHT_BATCH):
        guard.check(_payload(LIGHT_COMMANDS[i % len(LIGHT_COMMANDS)]))
    mean_ms = (time.process_time() - start) * 1000 / LIGHT_BATCH
    print("heavy-admission light-call process time: %.3f ms/call (n=%d)" % (mean_ms, LIGHT_BATCH))
    assert mean_ms < LIGHT_BAR_MS


def test_heavy_calls_spawn_nothing_across_the_classifier_table(no_spawn, monkeypatch, tmp_path):
    from coordinator_core.bash_guards import _heavy_lease_store as leases

    monkeypatch.setattr(leases, "leases_dir", lambda: tmp_path / "leases")
    from coordinator_core.bash_guards import _host_probe
    from coordinator_core.bash_guards._heavy_admission_contract import (
        KEY_FREE_RAM_FLOOR_MB,
        KEY_LEASE_RESERVE_MB,
        KEY_SESSION_BACKGROUND_CAP,
        KEY_SESSION_HEAVY_CAP,
    )

    reading = _host_probe.read_available_mb()
    assert reading.trusted, "real RAM reading untrusted; the RAM leg below would not be exercised"
    monkeypatch.setattr(
        guard,
        "_read_config",
        lambda: {
            KEY_FREE_RAM_FLOOR_MB: "1",
            KEY_LEASE_RESERVE_MB: "1",
            KEY_SESSION_HEAVY_CAP: "1",
            KEY_SESSION_BACKGROUND_CAP: "1",
        },
    )
    for command in HEAVY_COMMANDS:
        out = guard.check(_payload(command))
        assert out is None or "BLOCKED" in out["hookSpecificOutput"]["permissionDecisionReason"], command


def test_census_cost_is_measured_on_the_real_process_table(no_spawn):
    from coordinator_core.bash_guards import _host_probe, _session_census

    rows = _host_probe.snapshot()
    assert rows, "process table unreadable; the figure below would be meaningless"
    me = os.getpid()
    anchor = next((r for r in rows if r.pid == me), rows[0])
    census = _session_census.session_census(anchor, _host_probe.HostPrimitives)
    assert census is not None

    start = time.process_time()
    for _ in range(CENSUS_BATCH):
        taken = _host_probe.snapshot()
        assert taken
        assert _session_census.session_census(anchor, _host_probe.HostPrimitives, snapshot=taken) is not None
    mean_ms = (time.process_time() - start) * 1000 / CENSUS_BATCH
    print("heavy-admission census process time: %.3f ms/call (n=%d, rows=%d)" % (mean_ms, CENSUS_BATCH, len(rows)))
    assert mean_ms < CENSUS_BAR_MS
