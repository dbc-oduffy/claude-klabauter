"""The ack-journal prune never holds the dispatch-pool init lock.

A boot-time `prune_journal` stat-scan of a six-figure journal ran inline under
`_dispatch_pool_lock`, stalling every dispatch until it finished.
"""

from __future__ import annotations

import threading
import time

import pytest

from coordinator_core.warm import dispatch_ack, server

pytestmark = [pytest.mark.cadence]

_BOUND_S = 0.25


class _StubPool:
    def __init__(self, *_a, **_k):
        pass


def _bare_ctx(tmp_path, monkeypatch):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx.engine_root = tmp_path
    ctx._dispatch_pool = None
    ctx._dispatch_pool_lock = threading.Lock()
    monkeypatch.setattr(server, "_prune_worker_stderr_logs", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_prune_dispatch_claims", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_start_telemetry_drain", lambda *_a, **_k: threading.Event())
    monkeypatch.setattr(server.concurrent.futures, "ProcessPoolExecutor", _StubPool)
    monkeypatch.setattr(server._ack_store, "_journal_dir", None)
    return ctx


def test_pool_init_returns_while_journal_prune_is_blocked(tmp_path, monkeypatch):
    gate = threading.Event()
    entered = threading.Event()

    def _blocked_prune(*_a, **_k):
        entered.set()
        gate.wait(10)

    monkeypatch.setattr(dispatch_ack, "prune_journal", _blocked_prune)
    ctx = _bare_ctx(tmp_path, monkeypatch)
    result: list = []

    t = threading.Thread(target=lambda: result.append(ctx._ensure_dispatch_pool()), daemon=True)
    started = time.monotonic()
    t.start()
    try:
        t.join(_BOUND_S)
        assert not t.is_alive(), "pool init is blocked behind the journal prune"
        assert time.monotonic() - started < _BOUND_S
        assert isinstance(result[0], _StubPool)
    finally:
        gate.set()
        t.join(5)


def test_journal_prune_still_runs_after_pool_init(tmp_path, monkeypatch):
    ran = threading.Event()
    seen: list = []

    def _prune(journal_dir, *_a, **_k):
        seen.append(journal_dir)
        ran.set()

    monkeypatch.setattr(dispatch_ack, "prune_journal", _prune)
    ctx = _bare_ctx(tmp_path, monkeypatch)
    ctx._ensure_dispatch_pool()

    assert ran.wait(5), "journal prune never ran"
    assert seen == [server._ack_store._journal_dir]
    assert server._ack_store._journal_dir is not None


def test_fs_probe_returns_within_bound_while_the_open_is_blocked(tmp_path, monkeypatch):
    gate = threading.Event()
    real_open = server.os.open

    def _blocked_open(path, *a, **k):
        if ".probe-" in str(path):
            gate.wait(10)
        return real_open(path, *a, **k)

    monkeypatch.setattr(server.os, "open", _blocked_open)
    started = time.monotonic()
    try:
        out = server._fs_probe_ms(tmp_path)
        assert time.monotonic() - started < 1.0
        assert out == "error:timeout"
    finally:
        gate.set()


def test_fs_probe_still_reports_milliseconds_on_a_healthy_dir(tmp_path):
    out = server._fs_probe_ms(tmp_path)
    assert float(out) >= 0


def _journal_msg():
    return {"method": "dispatch.emit", "params": {}}


def test_journal_landed_returns_within_bound_while_record_outcome_is_blocked(tmp_path, monkeypatch):
    gate = threading.Event()
    landed: list = []
    real = dispatch_ack.record_outcome

    def _blocked(journal, key, outcome, **kw):
        gate.wait(10)
        landed.append(key)
        return real(journal, key, outcome, **kw)

    monkeypatch.setattr(dispatch_ack, "record_outcome", _blocked)
    monkeypatch.setattr(server, "_op_may_mutate", lambda _m: True)
    claim_dir = str(tmp_path / "claims")
    started = time.monotonic()
    try:
        server._journal_landed(_journal_msg(), claim_dir, "k1", {"result": {"ok": True}})
        assert time.monotonic() - started < 1.0
        assert landed == []
    finally:
        gate.set()
    for _ in range(100):
        if landed:
            break
        time.sleep(0.05)
    assert landed == ["k1"]


def test_journal_landed_still_journals_on_a_healthy_dir(tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(dispatch_ack, "record_outcome", lambda j, k, o, **kw: seen.append((k, o)))
    monkeypatch.setattr(server, "_op_may_mutate", lambda _m: True)
    server._journal_landed(_journal_msg(), str(tmp_path / "claims"), "k2", {"result": 1})
    assert seen == [("k2", dispatch_ack.OUTCOME_RESULT)]


def test_boot_timing_row_returns_within_bound_while_the_write_is_blocked(tmp_path, monkeypatch):
    gate = threading.Event()

    def _blocked(**_k):
        gate.wait(10)

    monkeypatch.setattr(server.telemetry, "record_server_boot", _blocked)
    started = time.monotonic()
    try:
        server._record_own_boot(1.0, 2.0, tmp_path)
        assert time.monotonic() - started < 1.0
    finally:
        gate.set()
