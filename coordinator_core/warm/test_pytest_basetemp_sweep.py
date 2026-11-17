"""Tests for the pytest basetemp sweeper; every root is a tmp_path, never the real temp dir."""
from __future__ import annotations

import getpass
import os
import threading

from coordinator_core.warm import pytest_basetemp_sweep as sw

NOW = 1_000_000_000.0
OLD = NOW - 7 * 3600
YOUNG = NOW - 3600


def _mk(tmp_path, name, mtime, lock=None, files=3):
    root = tmp_path / f"pytest-of-{getpass.getuser()}"
    d = root / name
    (d / "sub").mkdir(parents=True)
    for i in range(files):
        (d / "sub" / f"f{i}").write_text("x")
    if lock is not None:
        (d / ".lock").write_text(str(lock))
    os.utime(d, (mtime, mtime))
    return d


def _sweep(tmp_path, alive=lambda pid: False, **kw):
    sleeps = []
    report = sw.sweep_once(
        temp_root=str(tmp_path), now=NOW, pid_alive=alive, sleep=sleeps.append, **kw
    )
    return report, sleeps


def test_old_dead_lock_removed_and_young_kept(tmp_path):
    old = _mk(tmp_path, "pytest-1", OLD, lock=111)
    young = _mk(tmp_path, "pytest-2", YOUNG, lock=222)
    report, _ = _sweep(tmp_path)
    assert not old.exists()
    assert young.exists()
    assert report["removed"] == ["pytest-1"]
    assert report["kept"] == 1


def test_old_live_pid_dir_is_kept(tmp_path):
    live = _mk(tmp_path, "pytest-3", OLD, lock=333)
    dead = _mk(tmp_path, "pytest-4", OLD, lock=444)
    _sweep(tmp_path, alive=lambda pid: pid == 333)
    assert live.exists()
    assert not dead.exists()


def test_old_dir_without_lock_is_removed(tmp_path):
    d = _mk(tmp_path, "pytest-5", OLD)
    _sweep(tmp_path)
    assert not d.exists()


def test_non_basetemp_names_are_untouched(tmp_path):
    other = _mk(tmp_path, "keep-me", OLD)
    current = _mk(tmp_path, "pytest-current", OLD)
    _sweep(tmp_path)
    assert other.exists() and current.exists()


def test_deletes_pause_between_batches(tmp_path):
    _mk(tmp_path, "pytest-6", OLD, files=1)
    d = tmp_path / f"pytest-of-{getpass.getuser()}" / "pytest-6"
    for i in range(5):
        (d / f"extra{i}").write_text("x")
    os.utime(d, (OLD, OLD))  # creating children bumped mtime; restore age
    _, sleeps = _sweep(tmp_path, batch_size=2)
    assert not d.exists()
    assert len(sleeps) >= 3


def test_missing_root_is_a_quiet_noop(tmp_path):
    report, _ = _sweep(tmp_path)
    assert report == {"removed": [], "kept": 0, "failed": []}


def test_failure_is_caught_and_reported(tmp_path, monkeypatch):
    d = _mk(tmp_path, "pytest-7", OLD)

    def boom(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(sw, "_rmtree", boom)
    report, _ = _sweep(tmp_path)
    assert report["failed"] == ["pytest-7"]
    assert d.exists()


def test_loop_sweeps_at_start_then_waits_interval(tmp_path, monkeypatch):
    calls = []
    stop = threading.Event()

    def fake_sweep(**kw):
        calls.append(kw)
        stop.set()
        return {"removed": [], "kept": 0, "failed": []}

    monkeypatch.setattr(sw, "sweep_once", fake_sweep)
    sw.sweep_loop(stop=stop, interval_secs=3600, temp_root=str(tmp_path))
    assert len(calls) == 1
