"""A stalled verdict-cache create must not block the parse that produced it."""

from __future__ import annotations

import threading
import time

from coordinator_core.ops.dispatch_emit import landing_integrity as li


def test_a_stalled_cache_write_does_not_block_the_caller(tmp_path, monkeypatch):
    release = threading.Event()
    stored = threading.Event()

    def stalled_store(path, found):
        release.wait(10)
        stored.set()

    monkeypatch.setattr(li, "_store_verdict", stalled_store)
    monkeypatch.setattr(li, "_force_async", True)
    monkeypatch.setattr(li, "_write_queue", None)
    started = time.monotonic()
    li._write_verdict(tmp_path / "v.json", None)
    assert time.monotonic() - started < 1.0
    release.set()
    assert stored.wait(5)


def test_a_full_queue_drops_the_record(tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(li, "_store_verdict", lambda path, found: release.wait(10))
    monkeypatch.setattr(li, "_force_async", True)
    monkeypatch.setattr(li, "_write_queue", None)
    monkeypatch.setattr(li, "_WRITE_QUEUE_MAX", 1)
    started = time.monotonic()
    for i in range(5):
        li._write_verdict(tmp_path / f"v{i}.json", None)
    assert time.monotonic() - started < 1.0
    release.set()
