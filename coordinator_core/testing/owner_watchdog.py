"""pytest plugin: a pytest session dies within seconds of its owner, on every launch route.

At configure it watches the session's ancestor chain (shells such as `timeout` included, ending at
the first non-shell ancestor); when any watched ancestor dies or its pid is reused it kills this
session's own descendants and exits 137. Polls are one pid probe per ancestor: no process
enumeration. Under `scoped_pytest` the launcher already watches; this adds nothing harmful there.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

import psutil

from coordinator_core.testing import scoped_pytest, watch_parent

EXIT_OWNER_DIED = 137
WATCH_INTERVAL_S = 2.0
_started = False


def kill_descendants(root_pid: Optional[int] = None) -> None:
    """Kill every live descendant of `root_pid` (default: this process). psutil compares pid and
    create time on kill, so a reused pid is never hit."""
    try:
        children = psutil.Process(root_pid or os.getpid()).children(recursive=True)
    except psutil.Error:
        return
    for child in reversed(children):  # leaves first: a killed parent would orphan its children from our tree
        try:
            child.kill()
        except psutil.Error:
            pass


def start(
    chain: Optional[list] = None,
    interval: float = WATCH_INTERVAL_S,
    exit_fn: Optional[Callable[[int], None]] = None,
) -> list:
    """Start one watcher thread per (pid, create_time) in `chain` (default: this process's ancestor
    chain). On the first ancestor loss: kill own descendants, then `exit_fn(137)` (default
    `os._exit`, which skips finalizers). Returns the started threads."""
    if chain is None:
        chain = scoped_pytest.ancestor_chain()
    leave = exit_fn or os._exit

    def on_gone() -> None:
        kill_descendants()
        leave(EXIT_OWNER_DIED)

    return [watch_parent.watch_parent(pid, created, on_gone, interval) for pid, created in chain]


def pytest_configure(config) -> None:
    """Arm once per process; xdist workers arm their own chain, which ends at the controller."""
    global _started
    if _started:
        return
    _started = True
    start()
