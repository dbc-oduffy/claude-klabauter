"""resolve_request_repo caches realpath per raw string and never holds the thread on a blocked one."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from coordinator_core import ipc


@pytest.fixture(autouse=True)
def _fresh_cache():
    ipc._REALPATH_CACHE.clear()
    yield
    ipc._REALPATH_CACHE.clear()


def _count_resolves(monkeypatch, gate: "threading.Event | None" = None):
    calls: list[str] = []
    real = Path.resolve

    def counting(self, *a, **k):
        calls.append(str(self))
        if gate is not None:
            gate.wait(10)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", counting)
    return calls


def test_repeated_root_resolves_once(tmp_path, monkeypatch):
    calls = _count_resolves(monkeypatch)
    msg = {ipc._ORIGIN_WORKTREE_FIELD: str(tmp_path)}
    first = ipc.resolve_request_repo(msg)
    for _ in range(50):
        assert ipc.resolve_request_repo(msg) == first
    assert calls.count(str(tmp_path)) == 1
    assert first == tmp_path.resolve()


def test_distinct_roots_resolve_once_each(tmp_path, monkeypatch):
    calls = _count_resolves(monkeypatch)
    roots = [tmp_path / n for n in ("a", "b", "c")]
    for _ in range(5):
        for root in roots:
            ipc.resolve_request_repo({ipc._ORIGIN_WORKTREE_FIELD: str(root)})
    assert sorted(calls) == sorted(str(r) for r in roots)


def test_stalled_miss_returns_unresolved_absolute_and_is_not_cached(tmp_path, monkeypatch):
    gate = threading.Event()
    _count_resolves(monkeypatch, gate)
    monkeypatch.setattr(ipc, "_REALPATH_DEADLINE_SECS", 0.1)
    raw = str(tmp_path)
    got = ipc.resolve_request_repo({ipc._ORIGIN_WORKTREE_FIELD: raw})
    assert got == Path(os.path.abspath(raw))
    assert raw not in ipc._REALPATH_CACHE
    gate.set()
    monkeypatch.undo()
    # the overrun worker drains, then the next call resolves and caches the canonical value
    from coordinator_core import bounded_io

    for _ in range(300):
        if f"realpath:{raw}" not in bounded_io._inflight:
            break
        threading.Event().wait(0.01)
    again = ipc.resolve_request_repo({ipc._ORIGIN_WORKTREE_FIELD: raw})
    assert again == tmp_path.resolve()
    assert ipc._REALPATH_CACHE[raw] == tmp_path.resolve()


def test_absent_field_is_none_and_not_cached():
    assert ipc.resolve_request_repo({}) is None
    assert ipc.resolve_request_repo({ipc._ORIGIN_WORKTREE_FIELD: ""}) is None
    assert ipc._REALPATH_CACHE == {}


def test_remote_fallback_uses_cache(tmp_path, monkeypatch):
    calls = _count_resolves(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    got = [ipc.resolve_request_repo({}) for _ in range(3)]
    assert calls.count(str(tmp_path)) == 1
    assert got == [tmp_path.resolve()] * 3
