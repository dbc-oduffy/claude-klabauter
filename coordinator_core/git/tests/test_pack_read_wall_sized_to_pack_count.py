"""The read wall grows with the cached pack count and a stalled wall is retried once before the latch arms."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go

N_IDX = 400


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    clock = _Clock()
    monkeypatch.setattr(go, "_pack_clock", clock)
    go._PACK_STALL_UNTIL.clear()
    walls: list[float] = []
    keys: list[str] = []
    script: list[bool] = []

    def fake_run_bounded(key, work, deadline, default):
        walls.append(deadline)
        keys.append(key)
        return default if (script.pop(0) if script else False) else work()

    monkeypatch.setattr(go, "run_bounded", fake_run_bounded)
    yield walls, keys, script
    go._PACK_STALL_UNTIL.clear()
    go._PACK_LISTING_CACHE.clear()


def _list_packs(common: Path, n: int) -> None:
    names = [f"pack-{i:040d}.idx" for i in range(n)]
    go._PACK_LISTING_CACHE[str(common)] = (None, go._PackListing(common / "objects" / "pack", names, [0] * n))


def _read(common: Path):
    return go._under_read_wall("pack-read", common, "a" * 40, lambda: "payload")


def test_wall_is_the_base_without_a_cached_listing(env, tmp_path: Path) -> None:
    walls, _, _ = env
    assert _read(tmp_path) == "payload"
    assert walls == [go.PACK_READ_DEADLINE_SECS]


def test_wall_grows_with_the_cached_pack_count(env, tmp_path: Path) -> None:
    walls, _, _ = env
    _list_packs(tmp_path, N_IDX)
    assert _read(tmp_path) == "payload"
    assert walls == [pytest.approx(go.PACK_READ_DEADLINE_SECS + N_IDX * go.PACK_READ_PER_IDX_SECS)]
    assert walls[0] > go.PACK_READ_DEADLINE_SECS


def test_sizing_does_not_list_the_directory(env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _list_packs(tmp_path, N_IDX)
    # go.os is the global os module: an unscoped patch outlives the test and fails the session teardown's os.walk.
    with monkeypatch.context() as m:
        m.setattr(go.os, "scandir", lambda *a, **k: pytest.fail("sizing listed the pack dir"))
        wall = go._sized_wall(tmp_path)
    assert wall > go.PACK_READ_DEADLINE_SECS


def test_stall_once_then_success_returns_the_object_and_arms_nothing(env, tmp_path: Path) -> None:
    walls, keys, script = env
    _list_packs(tmp_path, N_IDX)
    script.append(True)
    assert _read(tmp_path) == "payload"
    assert len(walls) == 2 and walls[0] == walls[1]
    assert keys[0] != keys[1]  # the overrun worker still holds the first key
    assert go._PACK_STALL_UNTIL == {}


def test_stall_twice_raises_after_exactly_two_sized_walls_and_arms_the_latch(env, tmp_path: Path) -> None:
    walls, _, script = env
    _list_packs(tmp_path, N_IDX)
    script.extend([True, True])
    with pytest.raises(go.PackReadDeadline, match=go.PACK_READ_STALLED_ADVISORY):
        _read(tmp_path)
    sized = go.PACK_READ_DEADLINE_SECS + N_IDX * go.PACK_READ_PER_IDX_SECS
    assert walls == [pytest.approx(sized)] * 2
    assert str(tmp_path) in go._PACK_STALL_UNTIL
    with pytest.raises(go.PackReadDeadline, match="failing fast"):
        _read(tmp_path)
    assert len(walls) == 2


def test_final_raise_carries_pack_count_and_midx_lock_age(env, tmp_path: Path) -> None:
    _, _, script = env
    _list_packs(tmp_path, N_IDX)
    pack_dir = tmp_path / "objects" / "pack"
    pack_dir.mkdir(parents=True)
    lock = pack_dir / "multi-pack-index.lock"
    lock.write_bytes(b"")
    old = go.time.time() - 3600
    os.utime(lock, (old, old))
    script.extend([True, True])
    with pytest.raises(go.PackReadDeadline) as ei:
        _read(tmp_path)
    msg = str(ei.value)
    assert f"packs={N_IDX}" in msg
    assert "multi-pack-index.lock age=36" in msg


def test_final_raise_without_a_lock_names_no_lock(env, tmp_path: Path) -> None:
    _, _, script = env
    _list_packs(tmp_path, N_IDX)
    script.extend([True, True])
    with pytest.raises(go.PackReadDeadline) as ei:
        _read(tmp_path)
    assert f"packs={N_IDX}" in str(ei.value)
    assert "multi-pack-index.lock" not in str(ei.value)


@pytest.mark.deliberate_wall_clock(reason="a real stalled worker must overrun the wall before the retry can succeed, a wait CPU time cannot see")
def test_real_stall_once_then_success_at_default_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(go, "PACK_READ_DEADLINE_SECS", 0.2)
    go._PACK_STALL_UNTIL.clear()
    release = threading.Event()
    calls: list[int] = []

    def search(common_dir, sha, *, revalidate):
        calls.append(1)
        if len(calls) == 1:
            release.wait(10)
            return None
        return (("blob", b"ok"),)

    monkeypatch.setattr(go, "_search_packs_for_sha_unbounded", search)
    try:
        assert go._search_packs_for_sha(tmp_path, "d" * 40, revalidate=False) == (("blob", b"ok"),)
    finally:
        release.set()
        go._PACK_STALL_UNTIL.clear()
    assert len(calls) == 2
