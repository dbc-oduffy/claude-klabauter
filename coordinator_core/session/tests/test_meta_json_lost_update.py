"""meta.json read-modify-write is serialised: concurrent writers of distinct
fields never drop one another's update (update_meta_field / update_meta_fields).
"""

from __future__ import annotations

import json
import os
import threading
import time

from coordinator_core.session import core


def _seed(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"session_id": "x"}))


def test_concurrent_writers_keep_every_field(tmp_path, monkeypatch):
    _seed(tmp_path)
    real_replace = os.replace

    def slow_replace(src, dst):
        # Widens the read->replace window so an unserialised writer is certain
        # to have read the pre-mutation dict before a peer's replace lands.
        time.sleep(0.05)
        return real_replace(src, dst)

    monkeypatch.setattr(core.os, "replace", slow_replace)

    results = {}

    def single():
        results["goal"] = core.update_meta_field(str(tmp_path), "goal", "g")

    def batch():
        results["batch"] = core.update_meta_fields(
            str(tmp_path), {"last_activity": "t", "pid": 7}
        )

    threads = [threading.Thread(target=single), threading.Thread(target=batch)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == {"goal": True, "batch": True}
    data = json.loads((tmp_path / "meta.json").read_text())
    assert data == {"session_id": "x", "goal": "g", "last_activity": "t", "pid": "7"}
    assert not (tmp_path / core._META_LOCK_NAME).exists()


def test_writer_waits_for_held_lock(tmp_path, monkeypatch):
    _seed(tmp_path)
    # Stale/wait windows far above any scheduler stall: the lock is released by
    # rmdir below, never by the writer reaping it as stale or timing out.
    monkeypatch.setattr(core, "_META_LOCK_STALE_SECONDS", 60.0)
    monkeypatch.setattr(core, "_META_LOCK_WAIT_SECONDS", 60.0)
    lock_dir = tmp_path / core._META_LOCK_NAME
    lock_dir.mkdir()
    done = threading.Event()
    result = {}

    def write():
        result["ok"] = core.update_meta_field(str(tmp_path), "goal", "g")
        done.set()

    t = threading.Thread(target=write)
    t.start()
    assert not done.wait(0.2)  # blocked behind the live lock
    lock_dir.rmdir()
    assert done.wait(2.0)
    t.join()
    assert result["ok"] is True
    assert core.read_meta_field(str(tmp_path), "goal") == "g"


def test_stale_lock_from_crashed_holder_is_reaped(tmp_path):
    _seed(tmp_path)
    lock_dir = tmp_path / core._META_LOCK_NAME
    lock_dir.mkdir()
    old = time.time() - core._META_LOCK_STALE_SECONDS - 1
    os.utime(lock_dir, (old, old))
    assert core.update_meta_field(str(tmp_path), "goal", "g") is True
    assert core.read_meta_field(str(tmp_path), "goal") == "g"
    assert not lock_dir.exists()


def test_lock_timeout_reports_failure_and_writes_nothing(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(core, "_META_LOCK_WAIT_SECONDS", 0.05)
    lock_dir = tmp_path / core._META_LOCK_NAME
    lock_dir.mkdir()
    assert core.update_meta_fields(str(tmp_path), {"goal": "g"}) is False
    assert json.loads((tmp_path / "meta.json").read_text()) == {"session_id": "x"}
    assert lock_dir.exists()  # a timed-out waiter never releases a peer's lock


def test_lock_released_on_unreadable_meta(tmp_path):
    (tmp_path / "meta.json").write_text("{not json")
    assert core.update_meta_field(str(tmp_path), "goal", "g") is False
    assert not (tmp_path / core._META_LOCK_NAME).exists()
