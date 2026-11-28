"""`retire_worker` leaves no unlisted live worker when its helper thread cannot start."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from coordinator_core.warm import pool_retire


class _Proc:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.alive = True
        self.terminated = False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        return None


class _Lock:
    def __init__(self) -> None:
        self.held_during_pop = False
        self._held = False

    def __enter__(self) -> "_Lock":
        self._held = True
        return self

    def __exit__(self, *exc: object) -> None:
        self._held = False


class _Procs(dict):
    def __init__(self, lock: _Lock, *args: object) -> None:
        super().__init__(*args)
        self._lock = lock

    def pop(self, key, *default):
        self._lock.held_during_pop = self._lock._held
        return super().pop(key, *default)


def _pool(lock: _Lock, proc: _Proc) -> SimpleNamespace:
    return SimpleNamespace(
        _processes=_Procs(lock, {proc.pid: proc}),
        _safe_to_dynamically_spawn_children=True,
        _shutdown_lock=lock,
    )


def _no_thread(*args: object, **kwargs: object):
    raise RuntimeError("can't start new thread")


def test_start_raises_terminates_worker_inline(monkeypatch):
    lock, proc, done = _Lock(), _Proc(4242), []
    pool = _pool(lock, proc)
    ledger: list = []
    monkeypatch.setattr(pool_retire.threading, "Thread", _no_thread)

    assert pool_retire.retire_worker(pool, 4242, on_done=lambda: done.append(1), ledger=ledger) is True

    assert proc.terminated and not proc.alive
    assert 4242 not in pool._processes
    assert ledger == [proc]
    assert done == [1]


def test_pop_runs_under_the_executor_shutdown_lock():
    lock, proc = _Lock(), _Proc(7)
    pool = _pool(lock, proc)
    assert pool_retire.retire_worker(pool, 7) is True
    assert lock.held_during_pop is True


def test_thread_start_ok_still_terminates_on_helper_thread():
    lock, proc = _Lock(), _Proc(9)
    pool = _pool(lock, proc)
    done = threading.Event()
    assert pool_retire.retire_worker(pool, 9, on_done=done.set) is True
    assert done.wait(5.0)
    assert proc.terminated


def test_unknown_pid_touches_nothing():
    lock, proc = _Lock(), _Proc(1)
    pool = _pool(lock, proc)
    assert pool_retire.retire_worker(pool, 2) is False
    assert not proc.terminated and 1 in pool._processes
