"""A dispatch-pool worker does not outlive the server that spawned it.

`_ctx_shutdown` tears the pool down on the four sanctioned exit triggers that
reach `begin_shutdown` / `drain_and_exit`. A server that dies any OTHER way --
crash, `taskkill`, OOM -- never sends the pool its sentinel, and a worker
blocked on its call-queue `get()` waits forever for a parent that will never
speak again. Windows offers no parent-death signal and no process-group
teardown, so nothing reclaims it. Observed live 2026-08-19: two orphaned
workers whose parents had no `Win32_Process` row at all, each still holding a
console window, unreachable except by `taskkill`.

These tests pin the watchdog's two halves without spawning anything (the
end-to-end proof -- kill a real parent, watch real workers die -- is a
`spawns_process` cadence-tier concern, and was run by hand when this landed).

Negative-spec:
    - Does NOT assert the watchdog kills the process. `_exit_with_parent` ends
      in `os._exit`, which would take the test runner with it; the seam under
      test is `_wait_for_parent_exit`'s RETURN, which is what gates that call.
    - Does NOT spawn a process pool. See module docstring.

Spawn ratchet C2 disposition: TIER -- `_dead_pid()` needs a PID that is
genuinely, OS-level dead (module's own comment: a fixed constant risks
colliding with a live process on a 50-70-concurrent-session box), which
only a real spawn-then-reap can produce; the process identity itself is
load-bearing for `_wait_for_parent_exit`'s dead-parent branch, not
incidental scaffolding.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from coordinator_core.warm import server
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _dead_pid() -> int:
    """A PID that is certainly gone: spawn a trivial child, reap it, reuse it.

    Not a made-up constant -- a high fixed number risks colliding with a live
    process on a box running 50-70 concurrent sessions, which would hang the
    infinite wait rather than fail the assertion.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **no_console_creationflags(),
    )
    proc.wait(timeout=60)
    return proc.pid


def test_wait_for_parent_exit_returns_immediately_for_a_dead_parent() -> None:
    """The orphan case: the parent is ALREADY gone when the worker looks.

    On Windows this is the `OpenProcess` -> NULL branch. A watchdog that
    blocked here instead would leak exactly the process it exists to reap.
    """
    started = time.monotonic()
    server._wait_for_parent_exit(_dead_pid())
    assert time.monotonic() - started < 30.0


def test_wait_for_parent_exit_blocks_while_the_parent_is_alive() -> None:
    """The live case: the watchdog must not fire on a healthy server, or the
    pool would reap its own workers mid-dispatch.
    """
    thread = threading.Thread(
        target=server._wait_for_parent_exit, args=(os.getpid(),), daemon=True
    )
    thread.start()
    thread.join(timeout=2.0)
    assert thread.is_alive(), "watchdog returned while its parent was still running"


def test_worker_init_starts_the_watchdog_as_a_daemon_thread(monkeypatch) -> None:
    """`_worker_process_init` must arm the watchdog, not merely define it.

    Pinned because the registry preload is the eye-catching half of this
    initializer and an edit there could drop the watchdog without any other
    test noticing.
    """
    monkeypatch.setattr(server, "_preload_op_registry", lambda: None)
    started: list[threading.Thread] = []
    real_thread = threading.Thread

    class _CapturingThread(real_thread):  # type: ignore[misc,valid-type]
        def start(self):  # noqa: ANN201 -- test double
            started.append(self)

    monkeypatch.setattr(server.threading, "Thread", _CapturingThread)
    server._worker_process_init()

    assert len(started) == 1, "no watchdog thread was started"
    watchdog = started[0]
    assert watchdog.daemon is True
    assert watchdog._target is server._exit_with_parent  # type: ignore[attr-defined]


@pytest.mark.skipif(os.name != "nt", reason="Windows-only handle-wait branch")
def test_windows_branch_uses_a_handle_wait_not_a_poll() -> None:
    """The Windows path must cost one blocked thread, not a polling loop --
    50-70 concurrent sessions' worth of pool workers each waking on a timer is
    exactly the kind of idle cost this repo prices.
    """
    import inspect

    source = inspect.getsource(server._wait_for_parent_exit)
    windows_half = source.split("while os.getppid()")[0]
    assert "WaitForSingleObject" in windows_half
    assert "time.sleep" not in windows_half
