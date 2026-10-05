"""The -32004 envelope names which case fired: a pool worker that died
(`worker_died`: exit code or signal plus stderr tail) or a delivered op that
never answered (`no_reply`: op plus how long it waited).

The worker-death legs kill a REAL pool worker mid-op against a pool built by
`_ensure_dispatch_pool`, with the runtime base redirected into `tmp_path`;
no live warm engine is touched. A mutating op is never re-run after the death.
"""

from __future__ import annotations

import concurrent.futures
import os
import signal
import sys
import threading

import pytest

from coordinator_core.warm import breadcrumb, server
from coordinator_core.warm.client import WARM_DISPATCH_INDETERMINATE, _indeterminate_envelope

_MUTATING = "test.mutating_op"


def _dying_worker(msg, caller):
    sys.stderr.write("worker-about-to-die: marker-" + "x" * 5000 + "-TAIL\n")
    sys.stderr.flush()
    os._exit(7)


def _sigkilled_worker(msg, caller):
    sys.stderr.write("sigkill-marker\n")
    sys.stderr.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def _real_pool_ctx(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "runtime"))
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    ctx._dispatch_pool = None
    ctx._dispatch_pool_lock = threading.Lock()
    ctx.engine_root = tmp_path / "engine"
    return ctx


def _msg(method=_MUTATING):
    return {"jsonrpc": "2.0", "id": 11, "method": method, "params": {}}


def test_worker_exit_code_and_stderr_tail_reach_the_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_pool_dispatch_worker", _dying_worker)
    ctx = _real_pool_ctx(tmp_path, monkeypatch)
    try:
        out = ctx._pool_dispatch(_msg())
    finally:
        pool = ctx._dispatch_pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    error = out["error"]
    assert error["code"] == WARM_DISPATCH_INDETERMINATE
    diag = error["data"]["diagnosis"]
    assert diag["case"] == "worker_died"
    assert diag["op"] == _MUTATING
    assert diag["exitcode"] == 7
    assert diag["exit"] == "exit code 7"
    assert "worker-about-to-die" not in diag["stderr_tail"]  # head truncated
    assert diag["stderr_tail"].rstrip().endswith("-TAIL")
    assert len(diag["stderr_tail"].encode("utf-8")) <= server._WORKER_STDERR_TAIL_BYTES
    assert "worker_died" in error["message"]
    assert "exit code 7" in error["message"]
    assert "-TAIL" in error["message"]


def test_a_dead_worker_never_re_runs_the_mutating_op(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_pool_dispatch_worker", _dying_worker)
    monkeypatch.setattr(server, "_run_dispatch", lambda *a, **k: calls.append(1) or {})
    ctx = _real_pool_ctx(tmp_path, monkeypatch)
    try:
        out = ctx._pool_dispatch(_msg())
    finally:
        pool = ctx._dispatch_pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    assert out["error"]["code"] == WARM_DISPATCH_INDETERMINATE
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="signal exit codes are POSIX")
def test_worker_killed_by_signal_names_the_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_pool_dispatch_worker", _sigkilled_worker)
    ctx = _real_pool_ctx(tmp_path, monkeypatch)
    try:
        out = ctx._pool_dispatch(_msg())
    finally:
        pool = ctx._dispatch_pool
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    diag = out["error"]["data"]["diagnosis"]
    assert diag["exitcode"] == -signal.SIGKILL
    assert diag["exit"] == "signal SIGKILL"
    assert "sigkill-marker" in diag["stderr_tail"]


def test_dead_worker_diagnosis_without_a_pool_still_yields_the_case():
    diag = server._dead_worker_diagnosis(None, None)
    assert diag["case"] == "worker_died"
    envelope = server._pool_broken_indeterminate_envelope(_msg(), diag)
    assert envelope["error"]["code"] == WARM_DISPATCH_INDETERMINATE
    assert "no stderr captured" in envelope["error"]["message"]


class _StuckFuture:
    def result(self, timeout=None):
        raise concurrent.futures.TimeoutError()

    def cancel(self):
        return False

    def add_done_callback(self, fn):
        pass


class _StuckPool:
    def submit(self, *_a, **_k):
        return _StuckFuture()


def test_server_side_no_reply_names_op_and_wait(monkeypatch):
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._pool_outstanding = server.InFlightCounter()
    monkeypatch.setattr(ctx, "_ensure_dispatch_pool", lambda: _StuckPool(), raising=False)

    out = ctx._pool_dispatch(_msg())

    diag = out["error"]["data"]["diagnosis"]
    assert out["error"]["code"] == WARM_DISPATCH_INDETERMINATE
    assert diag["case"] == "no_reply"
    assert diag["op"] == _MUTATING
    assert diag["waited_secs"] == server._POOL_RESULT_DEADLINE_SECS
    assert repr(_MUTATING) in out["error"]["message"]


def test_client_no_reply_envelope_names_op_and_wait():
    envelope = _indeterminate_envelope(
        _msg("memo.draft"), "no response within 30.0s", "k-1", waited_secs=30.04
    )
    error = envelope["error"]
    assert error["code"] == WARM_DISPATCH_INDETERMINATE
    assert error["data"]["diagnosis"] == {
        "case": "no_reply",
        "op": "memo.draft",
        "waited_secs": 30.04,
        "stage": "no response within 30.0s",
    }
    assert error["data"]["dispatch_key"] == "k-1"
    assert "'memo.draft'" in error["message"]
    assert "waited 30.0s" in error["message"]
