"""A slow git object read returns or raises within one wall and never holds a pool worker.

Designed-red falsifier for the bounded-read work: each case reads through the default
`read_object` / `packed_contains` / `read_object_header` entry on a one-worker pool with a stall
shim on the blocking leg, a concurrent op queued behind it.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait

import pytest

from coordinator_core.benchmarks import object_read_stall_profile as bench
from coordinator_core.git import git_objects as go

WALL = 0.3
HOLD = 3.0


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    return bench.build_fixtures(go, tmp_path_factory.mktemp("object-read-fixtures"))


@pytest.fixture(autouse=True)
def _short_wall(monkeypatch):
    monkeypatch.setattr(go, "PACK_READ_DEADLINE_SECS", WALL)
    bench.clear_caches(go)
    yield
    bench.clear_caches(go)


def _read_on_one_worker_pool(work, stall):
    """Run `work` on a one-worker pool with a trivial op queued behind it; return both outcomes."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        t0 = time.monotonic()
        fut = pool.submit(work)
        probe = pool.submit(time.monotonic)
        wait([fut], timeout=WALL + 0.5)
        assert stall.entered.is_set(), "the read never reached the stall shim"
        assert fut.done(), "the read still holds the pool worker past one wall + 0.5s"
        elapsed = time.monotonic() - t0
        probe.result(timeout=1.0)
        assert stall.released_at is None, "the concurrent op answered only after the stall was released"
        return fut, elapsed
    finally:
        stall.release_now()
        pool.shutdown(wait=True)


def _assert_raises_deadline(work, stall):
    fut, elapsed = _read_on_one_worker_pool(work, stall)
    assert isinstance(fut.exception(), go.PackReadDeadline), f"expected PackReadDeadline, got {fut.exception()!r}"
    assert elapsed <= WALL + 0.5


def test_stalled_pack_read_raises_within_one_wall_and_frees_the_worker(fx) -> None:
    with bench.Stall(go, hold=HOLD, pack_path=fx.pack_path) as stall:
        _assert_raises_deadline(lambda: go.read_object(fx.common_dir, fx.pack_sha), stall)


def test_stalled_ref_delta_base_read_raises_within_one_wall(fx) -> None:
    with bench.Stall(go, hold=HOLD, read_path=fx.base_path) as stall:
        _assert_raises_deadline(lambda: go.read_object(fx.common_dir, fx.delta_sha), stall)


def test_stalled_large_loose_read_raises_within_one_wall(fx) -> None:
    with bench.Stall(go, hold=HOLD, read_path=fx.big_path) as stall:
        _assert_raises_deadline(lambda: go.read_object(fx.common_dir, fx.big_sha), stall)


def test_stalled_pack_listing_in_packed_contains_raises_within_one_wall(fx) -> None:
    with bench.Stall(go, hold=HOLD, listing=True) as stall:
        _assert_raises_deadline(lambda: go.packed_contains(fx.listing_common, fx.listing_sha), stall)


def test_header_only_probe_reports_type_and_size_without_inflating_the_payload(fx) -> None:
    with bench.InflateSpy() as spy:
        got = go.read_object_header(fx.common_dir, fx.big_sha)
    assert got == ("blob", fx.big_size)
    assert spy.bytes_out <= 4096


def test_header_only_probe_on_ref_delta_reads_delta_header_not_base_payload(fx) -> None:
    with bench.InflateSpy() as spy:
        got = go.read_object_header(fx.common_dir, fx.delta_sha)
    assert got == ("blob", len(fx.delta_payload))
    assert spy.bytes_out < len(fx.base_payload)


# Shim reachability: with a short hold the same reads complete, so each red case above is red
# because of the missing bound, not because the shim or the fixture missed the read path.


@pytest.mark.parametrize(
    "case",
    ["pack", "ref_delta_base", "loose"],
)
def test_stall_shim_sits_on_the_default_read_path_and_reads_decode(fx, case) -> None:
    if case == "pack":
        stall, sha, want = bench.Stall(go, hold=0.05, pack_path=fx.pack_path), fx.pack_sha, fx.pack_payload
    elif case == "ref_delta_base":
        stall, sha, want = bench.Stall(go, hold=0.05, read_path=fx.base_path), fx.delta_sha, fx.delta_payload
    else:
        stall, sha, want = bench.Stall(go, hold=0.05, read_path=fx.big_path), fx.big_sha, None
    with stall:
        got = go.read_object(fx.common_dir, sha)
    assert stall.entered.is_set()
    assert got is not None and got[0] == "blob"
    if want is not None:
        assert got[1] == want
    else:
        assert len(got[1]) == fx.big_size


def test_pack_listing_stall_shim_is_reached_by_packed_contains(fx) -> None:
    with bench.Stall(go, hold=0.05, listing=True) as stall:
        assert go.packed_contains(fx.listing_common, fx.listing_sha) is True
    assert stall.entered.is_set()


def test_unstalled_reads_return_without_the_shim_blocking(fx) -> None:
    t0 = time.monotonic()
    assert go.read_object(fx.common_dir, fx.pack_sha)[1] == fx.pack_payload
    assert go.read_object(fx.common_dir, fx.delta_sha)[1] == fx.delta_payload
    assert time.monotonic() - t0 < WALL
