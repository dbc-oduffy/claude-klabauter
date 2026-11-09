"""Pack read deadline: an expired window raises, one retry re-opens the pack, a second expiry raises."""

from __future__ import annotations

import os
import zlib
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go


class _Clock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


@pytest.fixture(autouse=True)
def _clear_deadline():
    go._PACK_DEADLINE.at = None
    yield
    go._PACK_DEADLINE.at = None


def _stream(payload: bytes) -> bytes:
    return zlib.compress(payload)


def test_deadline_constant_is_below_server_stall_dump_secs() -> None:
    from coordinator_core.warm import server

    assert go.PACK_READ_DEADLINE_SECS < server.STALL_DUMP_SECS


def test_healthy_read_is_byte_identical_without_deadline() -> None:
    payload = os.urandom(5000) + b"x" * 20000
    assert go._zlib_decompress_bounded(_stream(payload), 0, 100) == payload


def test_healthy_read_under_deadline_is_byte_identical(monkeypatch) -> None:
    payload = os.urandom(5000) + b"x" * 20000
    monkeypatch.setattr(go, "_pack_clock", _Clock(0.0))
    go._PACK_DEADLINE.at = 1000.0
    assert go._zlib_decompress_bounded(_stream(payload), 0, 100) == payload


def test_expired_deadline_between_windows_raises(monkeypatch) -> None:
    payload = os.urandom(20000)
    monkeypatch.setattr(go, "_pack_clock", _Clock(1.0))
    go._PACK_DEADLINE.at = 2.5  # first check passes (t=1), a later window check fails
    with pytest.raises(go.PackReadDeadline):
        go._zlib_decompress_bounded(_stream(payload), 0, 64)


def test_deadline_error_is_a_read_model_error() -> None:
    assert issubclass(go.PackReadDeadline, go._GitReadModelError)


def test_one_expiry_reopens_pack_and_retries(monkeypatch, tmp_path: Path) -> None:
    calls = []
    reopened = []

    def fake_at(common_dir, pack_path, pack_bytes, offset, _depth=0):
        calls.append(pack_bytes)
        if len(calls) == 1:
            raise go.PackReadDeadline("slow")
        return 3, b"ok"

    monkeypatch.setattr(go, "_read_pack_object_at", fake_at)
    monkeypatch.setattr(go, "_read_pack_bytes", lambda p: reopened.append(p) or b"fresh")
    out = go._read_pack_object_deadlined(tmp_path, tmp_path / "p.pack", b"stale", 0)
    assert out == (3, b"ok")
    assert calls == [b"stale", b"fresh"]
    assert reopened == [tmp_path / "p.pack"]
    assert go._PACK_DEADLINE.at is None


def test_second_expiry_raises_and_clears_deadline(monkeypatch, tmp_path: Path) -> None:
    n = []

    def fake_at(*a, **k):
        n.append(1)
        raise go.PackReadDeadline("slow")

    monkeypatch.setattr(go, "_read_pack_object_at", fake_at)
    monkeypatch.setattr(go, "_read_pack_bytes", lambda p: b"fresh")
    with pytest.raises(go.PackReadDeadline):
        go._read_pack_object_deadlined(tmp_path, tmp_path / "p.pack", b"stale", 0)
    assert len(n) == 2
    assert go._PACK_DEADLINE.at is None


def test_nested_read_inherits_deadline_and_does_not_retry(monkeypatch, tmp_path: Path) -> None:
    n = []

    def fake_at(*a, **k):
        n.append(1)
        raise go.PackReadDeadline("slow")

    monkeypatch.setattr(go, "_read_pack_object_at", fake_at)
    go._PACK_DEADLINE.at = 1e12
    with pytest.raises(go.PackReadDeadline):
        go._read_pack_object_deadlined(tmp_path, tmp_path / "p.pack", b"x", 0)
    assert len(n) == 1
    assert go._PACK_DEADLINE.at == 1e12


def test_healthy_path_does_not_reopen_pack(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(go, "_read_pack_object_at", lambda *a, **k: (3, b"ok"))
    monkeypatch.setattr(go, "_read_pack_bytes", lambda p: pytest.fail("reopened on healthy read"))
    assert go._read_pack_object_deadlined(tmp_path, tmp_path / "p.pack", b"x", 0) == (3, b"ok")


@pytest.fixture
def _short_wall(monkeypatch):
    monkeypatch.setattr(go, "PACK_READ_DEADLINE_SECS", 0.3)
    go._PACK_STALL_UNTIL.clear()
    go._OBJECT_CACHE.clear()
    yield
    go._PACK_STALL_UNTIL.clear()
    go._OBJECT_CACHE.clear()


def test_alternates_share_one_wall_not_one_each(monkeypatch, tmp_path: Path, _short_wall) -> None:
    import threading
    import time

    release = threading.Event()
    reads = []

    def slow_local(common_dir, sha):
        reads.append(common_dir)
        release.wait(30)
        return None

    monkeypatch.setattr(go, "_read_local_object", slow_local)
    monkeypatch.setattr(go, "_alternate_common_dirs", lambda c: [tmp_path / "a1", tmp_path / "a2"])
    t0 = time.monotonic()
    try:
        with pytest.raises(go.PackReadDeadline):
            go._read_object(tmp_path, "a" * 40)
        assert time.monotonic() - t0 < 0.3 * 2
        assert reads == [tmp_path]
        assert go._object_cache_get((str(tmp_path), "a" * 40)) is go._CACHE_MISS
    finally:
        release.set()


def test_nested_read_object_runs_inline_inside_the_wall(monkeypatch, tmp_path: Path, _short_wall) -> None:
    seen = []

    def local(common_dir, sha):
        seen.append((sha[0], getattr(go._PACK_WORKER, "inside", False)))
        if sha[0] == "a":
            return go._read_object(common_dir, "b" * 40)
        return ("blob", b"base")

    monkeypatch.setattr(go, "_read_local_object", local)
    assert go._read_object(tmp_path, "a" * 40) == ("blob", b"base")
    assert seen == [("a", True), ("b", True)]
    assert getattr(go._PACK_WORKER, "inside", False) is False


def test_cache_hit_stays_outside_the_wall(monkeypatch, tmp_path: Path, _short_wall) -> None:
    go._object_cache_put((str(tmp_path), "a" * 40), ("blob", b"x"))
    monkeypatch.setattr(go, "run_bounded", lambda *a, **k: pytest.fail("cache hit entered the wall"))
    assert go._read_object(tmp_path, "a" * 40) == ("blob", b"x")


def test_packed_contains_overrun_raises_not_false(monkeypatch, tmp_path: Path, _short_wall) -> None:
    import threading

    release = threading.Event()
    monkeypatch.setattr(go, "_locate_packed", lambda *a, **k: release.wait(30))
    try:
        with pytest.raises(go.PackReadDeadline):
            go.packed_contains(tmp_path, "a" * 40)
    finally:
        release.set()


def test_read_local_object_propagates_deadline_not_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(go, "_read_loose_object", lambda c, s: None)

    def boom(c, s):
        raise go.PackReadDeadline("slow")

    monkeypatch.setattr(go, "_read_pack_object_by_sha", boom)
    with pytest.raises(go.PackReadDeadline):
        go._read_local_object(tmp_path, "a" * 40)
