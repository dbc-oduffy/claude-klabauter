"""After a pool recycle, heavy admission is paced; hook-lane and light callers are never delayed,
and a request whose deadline expires in the backoff window gets the not-started outcome."""

from __future__ import annotations

import threading
import time

import pytest

from coordinator_core.warm import server

pytestmark = pytest.mark.complete

SPACING = 0.15
WINDOW = 5.0


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    monkeypatch.setattr(server, "_POST_RECYCLE_ADMIT_SPACING_SECS", SPACING)
    monkeypatch.setattr(server, "_POST_RECYCLE_BACKOFF_SECS", WINDOW)


def _grant_times(gate, count):
    times, lock = [], threading.Lock()

    def one():
        waited = gate.acquire(True, 5.0)
        assert waited is not None
        with lock:
            times.append(time.monotonic())
        gate.release()

    threads = [threading.Thread(target=one) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return sorted(times)


def test_heavy_grants_are_spaced_after_recycle():
    gate = server._AdmissionGate(4, 0)
    gate.note_recycle()
    times = _grant_times(gate, 4)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) == 4
    assert all(g >= SPACING * 0.8 for g in gaps), gaps


@pytest.mark.deliberate_wall_clock(reason="admission pacing is a wall-clock interval; that interval is the behaviour under test")
def test_no_pacing_without_recycle():
    gate = server._AdmissionGate(4, 0)
    start = time.monotonic()
    times = _grant_times(gate, 4)
    assert times[-1] - start < SPACING


@pytest.mark.deliberate_wall_clock(reason="admission pacing is a wall-clock interval; that interval is the behaviour under test")
def test_pacing_ends_after_window(monkeypatch):
    monkeypatch.setattr(server, "_POST_RECYCLE_BACKOFF_SECS", 0.05)
    gate = server._AdmissionGate(4, 0)
    gate.note_recycle()
    time.sleep(0.08)
    start = time.monotonic()
    times = _grant_times(gate, 4)
    assert times[-1] - start < SPACING


@pytest.mark.deliberate_wall_clock(reason="admission pacing is a wall-clock interval; that interval is the behaviour under test")
def test_hook_and_light_callers_are_not_delayed():
    gate = server._AdmissionGate(4, 2)
    gate.note_recycle()
    for kwargs in ({"hook": True}, {"light": True}):
        t0 = time.monotonic()
        assert gate.acquire(False, 5.0, **kwargs) == 0.0
        assert time.monotonic() - t0 < SPACING / 2
        gate.release(**kwargs)


@pytest.mark.deliberate_wall_clock(reason="admission pacing is a wall-clock interval; that interval is the behaviour under test")
def test_deadline_expiring_in_backoff_is_not_admitted():
    gate = server._AdmissionGate(4, 0)
    gate.note_recycle()
    assert gate.acquire(True, 5.0) == 0.0
    gate.release()
    t0 = time.monotonic()
    assert gate.acquire(True, SPACING / 3) is None
    assert time.monotonic() - t0 < SPACING * 2
    with gate._lock:
        assert gate._heavy_held == 0


def test_paced_wait_counts_against_the_deadline():
    gate = server._AdmissionGate(1, 0)
    gate.note_recycle()
    assert gate.acquire(True, 5.0) == 0.0
    gate.release()
    waited = gate.acquire(True, 5.0)
    assert waited is not None and waited >= SPACING * 0.8
    gate.release()


def test_recycle_stamps_the_gate(monkeypatch):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._dispatch_pool_lock = threading.Lock()
    pool = object()
    ctx._dispatch_pool = pool
    monkeypatch.setattr(server._ack_store, "reconcile_restarted", lambda keys: None)
    monkeypatch.setattr(ctx, "_write_recycle_line", lambda *a, **k: None, raising=False)
    gate = ctx._admission_gate()
    assert gate._recycled_at is None
    ctx._recycle_stalled_pool(pool, [])
    assert gate._recycled_at is not None
