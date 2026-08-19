"""Pool workers open no console, and nothing breaks when their stdio is gone.

Separate from `test_pool_worker_parent_watchdog.py` because the subject is
different: that file pins how a worker DIES, this one pins how it is SPAWNED
and what its missing stdio costs.

`ProcessPoolExecutor` exposes no `creationflags` seam, so the repo-wide
`win_portability.no_console_creationflags` discipline cannot reach it —
multiprocessing builds its own `CreateProcess` call, and a detached warm
server therefore got a brand-new console per worker. `_suppress_pool_worker_
consoles` swaps `sys.executable` for `pythonw.exe` before the pool is built.

THE INVARIANT THAT MATTERS MOST HERE. Under `pythonw` a worker's
`sys.stdout`/`sys.stderr` are both `None`, and the repo writes
`print(..., file=sys.stderr)` at 200+ call sites — including
`ops/fleet/_common.py::_setup_error`, on the refusal path of every fleet op.
A code-reviewer flagged (2026-08-19, slice B) that this must raise
`AttributeError` and turn a structured `exit_code: 1` refusal into an opaque
INTERNAL_ERROR. It does not, and the reason is worth pinning rather than
re-deriving: CPython's `print` treats a `file` argument of `None` as "use
`sys.stdout`", and when that is `None` too it returns without writing. The
suppression fix rests entirely on that, so it is tested here rather than
trusted.

Negative-spec:
    - Does NOT spawn a pool. The end-to-end proof (a detached parent spawning
      real workers, counting visible windows) is a `spawns_process` cadence
      concern; it was run by hand when this landed — 2 windows unsuppressed,
      0 suppressed.
    - Does NOT assert `pythonw.exe` exists. A Python install without it is
      valid; the resolver's contract is to leave `sys.executable` alone then,
      never to substitute a different interpreter.
"""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.warm import server


def test_suppression_points_multiprocessing_at_pythonw(monkeypatch) -> None:
    """The whole popup fix in one assertion: the interpreter multiprocessing
    spawns is the console-less one."""
    if os.name != "nt":
        pytest.skip("Windows-only console suppression")
    from pathlib import Path

    if not Path(sys.executable).with_name("pythonw.exe").is_file():
        pytest.skip("no pythonw.exe beside this interpreter")

    recorded: list[str] = []
    monkeypatch.setattr(server.multiprocessing, "set_executable", recorded.append)
    server._suppress_pool_worker_consoles()

    assert len(recorded) == 1, "the pool's interpreter was never redirected"
    assert Path(recorded[0]).name.lower() == "pythonw.exe"


def test_suppression_is_a_no_op_off_windows(monkeypatch) -> None:
    """No console to suppress and no `pythonw` to point at — touching
    `set_executable` there would be a portability defect, not a fix."""
    recorded: list[str] = []
    monkeypatch.setattr(server.os, "name", "posix")
    monkeypatch.setattr(server.multiprocessing, "set_executable", recorded.append)
    server._suppress_pool_worker_consoles()
    assert recorded == []


def test_stderr_print_is_a_silent_no_op_when_stdio_is_none(monkeypatch) -> None:
    """The load-bearing invariant: a suppressed worker's `print(file=sys.stderr)`
    must not raise.

    Uses monkeypatch so the streams are restored even if the assertion fails —
    a leaked `sys.stdout = None` would take the rest of the suite with it.
    """
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    print("this must not raise", file=sys.stderr, flush=True)


def test_fleet_setup_error_still_returns_its_envelope_with_no_stdio(monkeypatch) -> None:
    """The refusal path a suppressed pool worker actually takes.

    `_setup_error` writes to stderr unconditionally (the cold-caller channel)
    before `_emit_warm_diagnostic` (the warm one). If the stderr write raised,
    the warm channel would never run and the caller would get an opaque
    INTERNAL_ERROR instead of this envelope.
    """
    from coordinator_core.ops.fleet._common import _setup_error

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    envelope = _setup_error("send", False, "memo.send: scoped_to.sha does not resolve")

    assert envelope["exit_code"] == 1
    assert envelope["acted"] == []
