"""Pool size follows measured headroom: grows with free RAM/CPU, shrinks under memory, CPU and disk pressure."""

from __future__ import annotations

import sys

import pytest

from coordinator_core.warm import pool_sizing as ps
from coordinator_core.warm.pool_sizing import Headroom, dispatch_pool_size

GIB = 1024


def test_pool_grows_with_free_ram():
    small = dispatch_pool_size(Headroom(free_ram_mb=6 * GIB, ncpu=24, load=0.0))
    large = dispatch_pool_size(Headroom(free_ram_mb=30 * GIB, ncpu=24, load=0.0))
    assert small < large
    assert large > 4, "a roomy box must exceed the retired fixed cap of 4"


def test_pool_shrinks_under_memory_pressure():
    roomy = dispatch_pool_size(Headroom(free_ram_mb=30 * GIB, ncpu=24, load=0.0))
    tight = dispatch_pool_size(Headroom(free_ram_mb=ps.RAM_RESERVE_MB + 512, ncpu=24, load=0.0))
    assert tight < roomy
    assert tight == 2


def test_pool_never_below_one_even_with_no_ram():
    assert dispatch_pool_size(Headroom(free_ram_mb=0, ncpu=24, load=1.0, disk_queue=500)) == 1


def test_pool_bounded_by_cpu_count():
    assert dispatch_pool_size(Headroom(free_ram_mb=90 * GIB, ncpu=8, load=0.0)) == 8


def test_pool_shrinks_with_cpu_load():
    idle = dispatch_pool_size(Headroom(free_ram_mb=90 * GIB, ncpu=24, load=0.0))
    busy = dispatch_pool_size(Headroom(free_ram_mb=90 * GIB, ncpu=24, load=0.75))
    assert busy < idle
    assert busy == 6


def test_disk_queue_shrinks_pool_the_2026_11_04_incident():
    calm = dispatch_pool_size(Headroom(free_ram_mb=90 * GIB, ncpu=24, load=0.0, disk_queue=2))
    below = dispatch_pool_size(Headroom(free_ram_mb=90 * GIB, ncpu=24, load=0.0, disk_queue=ps.DISK_QUEUE_HIGH))
    incident = dispatch_pool_size(Headroom(free_ram_mb=90 * GIB, ncpu=24, load=0.0, disk_queue=242))
    assert calm == below == 24, "queue at or under the threshold leaves the pool alone"
    assert incident == 1


def test_unreadable_inputs_do_not_shrink():
    assert dispatch_pool_size(Headroom(free_ram_mb=30 * GIB, ncpu=24)) == dispatch_pool_size(
        Headroom(free_ram_mb=30 * GIB, ncpu=24, load=0.0, disk_queue=0)
    )


def test_sample_is_cached_within_ttl(monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return Headroom(free_ram_mb=1.0, ncpu=1)

    monkeypatch.setattr(ps, "_read_headroom", fake)
    monkeypatch.setattr(ps, "_cached", None)
    a = ps.sample_headroom(ttl=60)
    b = ps.sample_headroom(ttl=60)
    assert a is b and len(calls) == 1
    ps.sample_headroom(ttl=0)
    assert len(calls) == 2


def test_sample_never_raises(monkeypatch):
    def boom():
        raise OSError("nope")

    monkeypatch.setattr(ps, "_read_headroom", boom)
    monkeypatch.setattr(ps, "_cached", None)
    assert dispatch_pool_size(ps.sample_headroom(ttl=0)) >= 1


def test_live_sample_is_sane_and_fast():
    import time

    t = time.perf_counter()
    h = ps.sample_headroom(ttl=0)
    assert time.perf_counter() - t < 0.2
    assert h.ncpu >= 1 and h.free_ram_mb > 0
    assert dispatch_pool_size(h) >= 1


@pytest.mark.skipif(sys.platform != "win32", reason="IOCTL_DISK_PERFORMANCE is Windows-only")
def test_live_disk_queue_reads_a_number_for_the_cwd_drive():
    q = ps._disk_queue(ps._op_drive())
    assert q is None or q >= 0
