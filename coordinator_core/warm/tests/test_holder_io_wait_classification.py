"""Stalled pool holders classify io_wait (slow repo drive, younger than `_IO_WAIT_MAX_SECS`) or hung.

Spike result: the drive probe separates the two on its own (an injected slow probe vs a healthy one),
so no worker CPU delta qualifier is added. Tests build `_ServerContext` with `__new__`; no server starts.
"""

from __future__ import annotations

import concurrent.futures
import subprocess
import time

from coordinator_core.warm import server


def _ctx():
    return server._ServerContext.__new__(server._ServerContext)


def _track(ctx, op, root, age, guard=False):
    fut = concurrent.futures.Future()
    ctx._track_pool_task(fut, op, f"key-{op}", guard=guard, repo_root=root)
    row = ctx._pool_tasks[fut]
    ctx._pool_tasks[fut] = (row[0], time.monotonic() - age, row[2], row[3])
    return fut


def _probe(monkeypatch, ms_by_root):
    calls = []

    def fake(root):
        calls.append(str(root))
        return ms_by_root[str(root)]

    monkeypatch.setattr(server, "_holder_drive_probe_ms", fake)
    return calls


def test_slow_drive_holder_is_io_wait(monkeypatch):
    ctx = _ctx()
    _track(ctx, "a", "root-c", server.STALL_DUMP_SECS + 5)
    _probe(monkeypatch, {"root-c": server._SLOW_DRIVE_PROBE_MS + 1})
    assert ctx._classify_stalled_holders() == ([], ["a"])


def test_healthy_drive_holder_is_hung(monkeypatch):
    ctx = _ctx()
    _track(ctx, "a", "root-c", server.STALL_DUMP_SECS + 5)
    _probe(monkeypatch, {"root-c": 1.0})
    assert ctx._classify_stalled_holders() == (["a"], [])


def test_slow_drive_holder_past_ceiling_is_hung(monkeypatch):
    ctx = _ctx()
    _track(ctx, "a", "root-c", server._IO_WAIT_MAX_SECS + 1)
    _probe(monkeypatch, {"root-c": 5000.0})
    assert ctx._classify_stalled_holders() == (["a"], [])


def test_unknown_root_or_unknown_probe_is_hung(monkeypatch):
    ctx = _ctx()
    _track(ctx, "norepo", None, server.STALL_DUMP_SECS + 5)
    _track(ctx, "noprobe", "root-d", server.STALL_DUMP_SECS + 5)
    _probe(monkeypatch, {"root-d": None})
    hung, io_wait = ctx._classify_stalled_holders()
    assert sorted(hung) == ["noprobe", "norepo"] and io_wait == []


def test_same_set_as_stalled_holders_guard_and_young_excluded(monkeypatch):
    ctx = _ctx()
    _track(ctx, "old", "root-c", server.STALL_DUMP_SECS + 5)
    _track(ctx, "young", "root-c", 0.0)
    _track(ctx, "guard", "root-c", server.STALL_DUMP_SECS + 5, guard=True)
    _probe(monkeypatch, {"root-c": 500.0})
    hung, io_wait = ctx._classify_stalled_holders()
    assert sorted(hung + io_wait) == sorted(ctx._stalled_holders()) == ["old"]


def test_four_holders_on_one_drive_probe_once_and_cost_under_a_millisecond(monkeypatch):
    ctx = _ctx()
    for i in range(4):
        _track(ctx, f"op{i}", "root-c", server.STALL_DUMP_SECS + 5)
    calls = _probe(monkeypatch, {"root-c": 500.0})
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    t0 = time.process_time()
    hung, io_wait = ctx._classify_stalled_holders()
    cost = time.process_time() - t0
    assert (hung, len(io_wait)) == ([], 4)
    assert calls == ["root-c"] and spawned == []
    assert cost < 0.001


def test_real_probe_on_a_tmp_dir_is_healthy(tmp_path):
    ms = server._holder_drive_probe_ms(tmp_path)
    assert ms is not None and ms < server._SLOW_DRIVE_PROBE_MS


def test_drive_verdict_stall_reads_as_infinitely_slow(monkeypatch):
    monkeypatch.setattr(server, "_drive_verdict", lambda root: {"case": "drive_stall"})
    assert server._holder_drive_probe_ms("root-x") == float("inf")
    monkeypatch.setattr(server, "_drive_verdict", lambda root: {"case": "enospc"})
    assert server._holder_drive_probe_ms("root-x") is None
