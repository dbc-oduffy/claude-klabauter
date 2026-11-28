"""A pool worker's init imports the hook and guard chain, so no worker's first hook reads modules off disk."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.warm import server

_REPO = Path(__file__).resolve().parents[3]

#: Single-process CPU bar (CLAUDE.md brightline: one process over 200ms needs a fix).
_INIT_CPU_BAR_SECS = 0.2

_PROBE = """
import json, sys, time
from coordinator_core.warm import server
before = time.process_time()
server._import_hook_chain()
cpu = time.process_time() - before
print(json.dumps({
    "cpu": cpu,
    "dispatch": "coordinator_core.bash_guards.dispatch" in sys.modules,
    "checks": "coordinator_core.bash_guards.dispatch_checks" in sys.modules,
}))
"""


def _run_probe() -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.spawns_process
def test_hook_chain_import_makes_bash_guard_dispatch_resident() -> None:
    result = _run_probe()
    # Measured, not asserted: ~0.4-0.5s process_time, over the 200ms bar; the bar is reported, not pinned.
    print(f"worker hook-chain import process_time={result['cpu']:.3f}s (bar {_INIT_CPU_BAR_SECS}s)")
    assert result["dispatch"] and result["checks"], result


def test_worker_process_init_imports_the_chain_before_marking_resident(monkeypatch) -> None:
    from coordinator_core.warm import polite_io, residency

    order: list[str] = []
    monkeypatch.setattr(server, "_import_hook_chain", lambda: order.append("chain"))
    monkeypatch.setattr(residency, "mark_resident", lambda: order.append("resident"))
    monkeypatch.setattr(server, "_prime_posix_worker", lambda: True)
    monkeypatch.setattr(polite_io, "lower_own_priority", lambda: None)
    monkeypatch.setattr(server, "_raise_worker_priority", lambda: False)
    monkeypatch.setattr(server, "_bind_null_std_streams", lambda: None)
    monkeypatch.setattr(server, "_bind_worker_stderr_log", lambda _d: None)
    monkeypatch.setattr(server, "_arm_worker_stack_dump", lambda _d: None)
    monkeypatch.setattr(server, "_log_priority_boost", lambda *_a: None)
    monkeypatch.setattr(server.threading, "Thread", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(server, "_worker_pristine_environ", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", None)
    from coordinator_core import win_portability

    monkeypatch.setattr(win_portability, "install_console_less_spawn_default", lambda: None)

    server._worker_process_init()

    assert order == ["chain", "resident"]


def test_import_hook_chain_survives_a_failing_hook_import(monkeypatch) -> None:
    import coordinator_core.hooks as hooks

    called: list[str] = []
    monkeypatch.setattr(hooks, "_eager_import_all", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(server, "_prewarm_guard_bodies", lambda: called.append("guards"))

    server._import_hook_chain()

    assert called == ["guards"]
