"""A pack read on a stalled drive returns `PackReadDeadline` within the wall, then fails fast."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go


@pytest.fixture(autouse=True)
def _short_wall(monkeypatch):
    monkeypatch.setattr(go, "PACK_READ_DEADLINE_SECS", 0.3)
    go._PACK_STALL_UNTIL.clear()
    yield
    go._PACK_STALL_UNTIL.clear()


def test_blocked_pack_read_raises_named_deadline_within_wall(monkeypatch, tmp_path: Path) -> None:
    release = threading.Event()
    monkeypatch.setattr(go, "_search_packs_for_sha_unbounded", lambda *a, **k: release.wait(30))
    t0 = time.monotonic()
    try:
        with pytest.raises(go.PackReadDeadline, match=go.PACK_READ_STALLED_ADVISORY):
            go._search_packs_for_sha(tmp_path, "a" * 40, revalidate=False)
        assert time.monotonic() - t0 < 2.0
    finally:
        release.set()


def test_second_read_after_stall_fails_fast_without_new_wait(monkeypatch, tmp_path: Path) -> None:
    release = threading.Event()
    monkeypatch.setattr(go, "_search_packs_for_sha_unbounded", lambda *a, **k: release.wait(30))
    try:
        with pytest.raises(go.PackReadDeadline):
            go._search_packs_for_sha(tmp_path, "a" * 40, revalidate=False)
        t0 = time.monotonic()
        with pytest.raises(go.PackReadDeadline, match="failing fast"):
            go._search_packs_for_sha(tmp_path, "b" * 40, revalidate=False)
        assert time.monotonic() - t0 < 0.1
    finally:
        release.set()


def test_healthy_read_returns_result_and_nested_read_runs_inline(monkeypatch, tmp_path: Path) -> None:
    seen = []

    def fake(common_dir, sha, *, revalidate):
        seen.append(getattr(go._PACK_WORKER, "inside", False))
        if sha == "a" * 40:
            return go._search_packs_for_sha(common_dir, "b" * 40, revalidate=revalidate)
        return (("blob", b"ok"),)

    monkeypatch.setattr(go, "_search_packs_for_sha_unbounded", fake)
    assert go._search_packs_for_sha(tmp_path, "a" * 40, revalidate=False) == (("blob", b"ok"),)
    assert seen == [True, True]
    assert getattr(go._PACK_WORKER, "inside", False) is False


def test_missing_object_is_none_not_a_deadline(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(go, "_search_packs_for_sha_unbounded", lambda *a, **k: None)
    assert go._search_packs_for_sha(tmp_path, "a" * 40, revalidate=False) is None


def test_read_error_inside_wall_propagates(monkeypatch, tmp_path: Path) -> None:
    def boom(*a, **k):
        raise go._GitReadModelError("corrupt")

    monkeypatch.setattr(go, "_search_packs_for_sha_unbounded", boom)
    with pytest.raises(go._GitReadModelError, match="corrupt"):
        go._search_packs_for_sha(tmp_path, "a" * 40, revalidate=False)


def test_cooldown_expires_and_reads_resume(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(go, "_search_packs_for_sha_unbounded", lambda *a, **k: (("blob", b"x"),))
    go._PACK_STALL_UNTIL[str(tmp_path)] = time.monotonic() - 1.0
    assert go._search_packs_for_sha(tmp_path, "c" * 40, revalidate=False) == (("blob", b"x"),)
