
from __future__ import annotations

import multiprocessing
import subprocess
import sys

import pytest

from coordinator_core.telemetry import spawn_counter
from coordinator_core.win_portability import no_console_creationflags


def _noop() -> None:
    return None


def _audit_popen() -> None:
    sys.audit("subprocess.Popen", "exe", ["exe"], None, None)


def test_audit_hook_is_installed_at_import():
    assert spawn_counter.audit_hook_installed() is True


def test_a_non_git_spawn_is_counted():
    before = spawn_counter.spawn_count()
    _audit_popen()
    assert spawn_counter.spawn_count() - before == 1


def test_os_system_is_counted():
    before = spawn_counter.spawn_count()
    sys.audit("os.system", b"echo hi")
    assert spawn_counter.spawn_count() - before == 1


def test_an_unaudited_hot_event_is_not_counted():
    before = spawn_counter.spawn_count()
    for _ in range(50):
        sys.audit("open", "/nonexistent", "r", 0)
    assert spawn_counter.spawn_count() == before


def test_git_is_not_double_counted():
    before = spawn_counter.spawn_count()
    _audit_popen()
    if not spawn_counter.audit_hook_installed():
        spawn_counter.bump()
    assert spawn_counter.spawn_count() - before == 1


def test_git_fallback_bumps_when_hook_not_installed(monkeypatch):
    monkeypatch.setattr(spawn_counter, "_hook_installed", False)
    before = spawn_counter.spawn_count()
    if not spawn_counter.audit_hook_installed():
        spawn_counter.bump()
    assert spawn_counter.spawn_count() - before == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_real_subprocess_counts_exactly_once():
    before = spawn_counter.spawn_count()
    subprocess.run(
        [sys.executable, "-c", "pass"],
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    assert spawn_counter.spawn_count() - before == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_failed_spawn_still_counts():
    """Counts ATTEMPTS: a child killed on timeout paid process-creation cost.

    CLAUDE.md § The brightline — "process creation is the cost, not the query".
    Counting only the success path would hide exactly the timeouts worth
    finding.
    """
    before = spawn_counter.spawn_count()
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            capture_output=True,
            timeout=0.3,
            **no_console_creationflags(),
        )
    assert spawn_counter.spawn_count() - before == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_multiprocessing_worker_is_not_counted():
    ctx = multiprocessing.get_context("spawn")
    before = spawn_counter.spawn_count()
    proc = ctx.Process(target=_noop)
    proc.start()
    proc.join(timeout=30)
    assert spawn_counter.spawn_count() == before
