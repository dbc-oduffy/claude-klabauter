"""Tests for T1's worker-pool depth read (docs/plans/2026-09-06-warm-
engine-survival-and-door-measurement.md § T1).

AC1's instrument: `census:` established that no depth read existed on
either `warm/telemetry.py` or `warm/breadcrumb.py` at HEAD (0 and 0), so
F4's py-spy substitution landed on an absent surface. This file pins two
things: (1) a freshly booted `WORKER_POOL_SIZE`-sized pool reports 30 live
threads via `_ServerContext.worker_pool_depth()`, and (2) a death route
that bypasses `_worker_loop`'s own `except Exception` guard (a
`SystemExit` escape) is actually detected as a depth drop -- not merely a
smaller `pool_size` argument, which would pin the counter's arithmetic
without demonstrating detection of a real die-off.
"""

from __future__ import annotations

import threading
import time

import pytest

from coordinator_core.warm import server, telemetry

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


class _FakeVersionState:
    def __init__(self, *, server_sha: str = "deadbeef"):
        self.server_sha = server_sha

    def is_skewed(self, client_token: str) -> bool:
        return False


def test_worker_pool_depth_reports_full_pool_size_on_fresh_boot():
    """A freshly started pool of `WORKER_POOL_SIZE` workers reports 30 live
    threads -- the fresh-boot baseline `worker_pool_depth()` exists to
    measure against."""
    ctx = server._ServerContext(name="pipe-depth", sid="sid-depth", version_state=_FakeVersionState())
    ctx._start_worker_pool()

    deadline = time.monotonic() + 5
    while ctx.worker_pool_depth() < server.WORKER_POOL_SIZE and time.monotonic() < deadline:
        time.sleep(0.01)

    assert ctx.worker_pool_depth() == server.WORKER_POOL_SIZE == 30


def test_worker_pool_depth_detects_a_guard_bypassing_die_off(monkeypatch):
    """A death route that escapes `_worker_loop`'s own `except Exception`
    guard -- here, a monkeypatched `_handle_connection` raising
    `SystemExit`, a `BaseException` subclass `except Exception` does not
    catch -- must actually be reflected as a depth drop. This is the
    negative-arm T1's spec requires: not `_start_worker_pool(pool_size=n<30)`,
    which only pins the counter's arithmetic, but a demonstrated real
    die-off the instrument can see.
    """

    def _fake_handle_connection(io, **kwargs):
        if io == "die":
            raise SystemExit("simulated death route past the except Exception guard")

    monkeypatch.setattr(server, "_handle_connection", _fake_handle_connection)

    ctx = server._ServerContext(name="pipe-die", sid="sid-die", version_state=_FakeVersionState())
    ctx._start_worker_pool(pool_size=3)

    deadline = time.monotonic() + 5
    while ctx.worker_pool_depth() < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ctx.worker_pool_depth() == 3

    ctx._enqueue_connection("die")

    deadline = time.monotonic() + 5
    while ctx.worker_pool_depth() == 3 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert ctx.worker_pool_depth() == 2  # the die-off is visible as a drop


def test_idle_tick_records_worker_pool_depth_through_telemetry(tmp_path, monkeypatch):
    """`_idle_tick` records the live depth through
    `telemetry.record_worker_pool_depth`, readable back via
    `telemetry.worker_pool_depth_samples` -- the existing
    `record_server_boot`/`server_boot_samples` append-log pattern T1's body
    names, sampled on the idle watchdog's own bounded tick."""
    from coordinator_core.warm import idle, lifecycle

    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: False)

    ctx = server._ServerContext(
        name="pipe-depth-telemetry",
        sid="sid-depth-telemetry",
        version_state=_FakeVersionState(),
        engine_root=tmp_path,
    )
    ctx._start_worker_pool(pool_size=2)

    deadline = time.monotonic() + 5
    while ctx.worker_pool_depth() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    ctx._idle_tick()

    samples = telemetry.worker_pool_depth_samples(tmp_path)
    assert samples, "expected at least one recorded worker-pool-depth row"
    assert samples[-1]["depth"] == 2

    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
