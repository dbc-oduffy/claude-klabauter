"""An op whose worker is blocked in a file create/open does not hold its admission slot past its budget.

The held future never resolves (the worker survives terminate while blocked in the kernel); the caller is
answered -32004 naming the stall dir, and the slot frees while the call is still blocked.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import io_probe, server

_OP = "dispatch.ask_stage"
_BUDGET_SECS = 0.4


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _Pool:
    def __init__(self) -> None:
        self.futures: list = []
        self._processes: dict = {}

    def submit(self, *_a, **_k):
        fut = concurrent.futures.Future()
        fut.set_running_or_notify_cancel()
        self.futures.append(fut)
        return fut

    def shutdown(self, wait=True, cancel_futures=False):
        pass


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(io_probe, "arm", lambda *_a, **_k: False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(server, "_withdrawal_deadline_secs", lambda *_a, **_k: _BUDGET_SECS)
    ctx = server._ServerContext(name="pipe-io", sid="sid-io", version_state=_VersionState(), engine_root=tmp_path)
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._stall_dir = tmp_path / "stall-dumps"
    ctx._stall_dir.mkdir()
    pool = _Pool()
    ctx._dispatch_pool = pool
    return ctx, pool


def _call(ctx, key: str, out: list) -> threading.Thread:
    server._ack_store.admit(key, _OP)
    # The worker took the claim before it blocked: the op started, so the deadline is the indeterminate path.
    server._claim_path(ctx._claim_dir, key).write_text("424242", encoding="ascii")
    thread = threading.Thread(
        target=lambda: out.append(ctx._pool_dispatch({"jsonrpc": "2.0", "id": 5, "method": _OP, "params": {}}, dispatch_key=key)),
        daemon=True,
    )
    thread.start()
    return thread


def _wait(predicate, secs: float) -> bool:
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_an_io_blocked_holder_past_budget_frees_its_slot_while_still_blocked(rig) -> None:
    ctx, pool = rig
    out: list = []
    thread = _call(ctx, "c6-io-blocked", out)
    gate = ctx._admission_gate()
    assert _wait(lambda: bool(pool.futures) and gate._heavy_held == 1, 5.0), "the holder never took a slot"

    thread.join(_BUDGET_SECS + 5.0)
    assert out, "the caller was never answered within the budget"
    assert out[0]["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
    assert not pool.futures[0].done(), "the worker must still be blocked for this to prove the release"
    assert gate._heavy_held == 0, "the I/O-blocked holder still holds its slot"


def test_the_timeout_names_the_stall_dir(rig) -> None:
    ctx, _pool = rig
    out: list = []
    _call(ctx, "c6-io-named", out).join(_BUDGET_SECS + 5.0)

    error = out[0]["error"]
    assert error["data"]["stall_dir"] == str(ctx._stall_dir)
    assert f"Stall dir: {ctx._stall_dir}." in error["message"]


def test_a_holder_inside_its_budget_keeps_its_slot(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_withdrawal_deadline_secs", lambda *_a, **_k: 30.0)
    ctx, pool = rig
    out: list = []
    thread = _call(ctx, "c6-io-inside", out)
    gate = ctx._admission_gate()
    try:
        assert _wait(lambda: bool(pool.futures) and gate._heavy_held == 1, 5.0)
        time.sleep(_BUDGET_SECS)
        assert not out and gate._heavy_held == 1, "a holder inside its budget lost its slot"
    finally:
        pool.futures[0].set_result({"jsonrpc": "2.0", "id": 5, "result": {"ok": True}})
        thread.join(10.0)
    assert out and "result" in out[0]
    assert gate._heavy_held == 0
