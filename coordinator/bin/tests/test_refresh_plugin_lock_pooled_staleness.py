"""test_refresh_plugin_lock_pooled_staleness — pytest coverage for the
owner-token/age-bounded lock in refresh-plugin-live-install.py.

Spec backlink: cross-repo/inbox/2026-09-02-example-game-repo-em-refresh-live-install-
warm-worker.md (Defect 2) — under `coordinator_core/warm/server.py` pool
worker, process lifetime != operation lifetime, so PID-liveness-only
staleness can never go true and `atexit`-only release never fires. Fixed by
(1) a `finally`-scoped explicit release keyed to a per-acquire owner token,
and (2) an age bound (`MAX_LOCK_AGE_SECONDS`) that can declare a lock stale
even while its recorded PID stays alive.
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest


_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "refresh_plugin_live_install_lock_test",
        _BIN_DIR / "refresh-plugin-live-install.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def test_acquire_then_release_clears_lock_dir(tmp_path):
    lock_dir = tmp_path / ".refresh-x.lock.d"
    owner = _mod._acquire_lock(lock_dir)
    assert lock_dir.is_dir()
    record = json.loads((lock_dir / "pid").read_text(encoding="utf-8"))
    assert record["owner"] == owner
    assert record["pid"] == os.getpid()
    assert isinstance(record["acquired_at"], float)

    _mod._release_lock(lock_dir, owner)
    assert not lock_dir.exists()


def test_release_with_stale_owner_token_is_a_noop(tmp_path):
    """A release call carrying an owner token that no longer matches the
    lock's current record (e.g. a `finally` firing after the lock was
    already reclaimed as stale by a later invocation) must not delete a
    lock it does not own."""
    lock_dir = tmp_path / ".refresh-y.lock.d"
    lock_dir.mkdir()
    _mod._write_lock_record(lock_dir, "someone-else")

    _mod._release_lock(lock_dir, "not-the-owner")
    assert lock_dir.is_dir()


def test_dead_pid_with_fresh_timestamp_is_stale():
    dead_pid = 999999  # assume-unallocated on a fresh test host
    record = {"owner": "x", "pid": dead_pid, "acquired_at": time.time()}
    stale, reason = _mod._lock_is_stale(record)
    assert stale
    assert "dead PID" in reason


def test_alive_pid_but_old_timestamp_is_stale_under_pooling():
    """This is the exact case a pooled warm-server worker produces: the
    recorded PID (the pool worker) stays alive forever, so only the age
    term can ever mark the lock stale."""
    own_pid = os.getpid()  # definitely alive
    old_ts = time.time() - (_mod.MAX_LOCK_AGE_SECONDS + 60)
    record = {"owner": "x", "pid": own_pid, "acquired_at": old_ts}
    stale, reason = _mod._lock_is_stale(record)
    assert stale
    assert "age" in reason


def test_alive_pid_and_fresh_timestamp_is_not_stale():
    own_pid = os.getpid()
    record = {"owner": "x", "pid": own_pid, "acquired_at": time.time()}
    stale, reason = _mod._lock_is_stale(record)
    assert not stale


def test_acquire_recovers_a_pool_worker_style_stuck_lock(tmp_path):
    """End-to-end: a lock whose PID stays alive (simulated with our own PID,
    standing in for an immortal pool worker) but whose age exceeds the
    bound must be auto-recoverable by a fresh `_acquire_lock` call, with no
    manual `rm -rf` required."""
    lock_dir = tmp_path / ".refresh-z.lock.d"
    lock_dir.mkdir()
    stuck_ts = time.time() - (_mod.MAX_LOCK_AGE_SECONDS + 120)
    _mod._write_lock_record(lock_dir, "stuck-owner")
    (lock_dir / "pid").write_text(
        json.dumps({"owner": "stuck-owner", "pid": os.getpid(), "acquired_at": stuck_ts}),
        encoding="utf-8",
    )

    new_owner = _mod._acquire_lock(lock_dir)
    assert new_owner != "stuck-owner"
    record = json.loads((lock_dir / "pid").read_text(encoding="utf-8"))
    assert record["owner"] == new_owner

    _mod._release_lock(lock_dir, new_owner)
