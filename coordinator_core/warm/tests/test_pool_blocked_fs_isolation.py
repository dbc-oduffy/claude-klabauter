"""One kernel-blocked filesystem call on the overdue path never holds the pool.

The request thread's claim create and the overdue kill's fence write run bounded: a call that does not
answer reads as started (create) or no worker found (kill), the caller is answered, the admission slot
frees, and a peer op completes beside the still-blocked call. No wall-clock assertion: waits are
polls on state, never on elapsed time.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import io_probe, server

_OP = "dispatch.ask_stage"
_BUDGET_SECS = 0.3
_PID = 424242


class _VersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


class _Proc:
    def is_alive(self) -> bool:
        return True

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class _Pool:
    def __init__(self) -> None:
        self.futures: list = []
        self._processes: dict = {_PID: _Proc()}

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
    ctx = server._ServerContext(name="pipe-blk", sid="sid-blk", version_state=_VersionState(), engine_root=tmp_path)
    ctx._claim_dir = tmp_path / "claims"
    ctx._claim_dir.mkdir()
    ctx._stall_dir = tmp_path / "stall-dumps"
    ctx._stall_dir.mkdir()
    pool = _Pool()
    ctx._dispatch_pool = pool
    return ctx, pool


def _call(ctx, key: str, out: list, *, claimed: bool) -> threading.Thread:
    server._ack_store.admit(key, _OP)
    if claimed:
        server._claim_path(ctx._claim_dir, key).write_text(str(_PID), encoding="ascii")
    thread = threading.Thread(
        target=lambda: out.append(
            ctx._pool_dispatch({"jsonrpc": "2.0", "id": 7, "method": _OP, "params": {}}, dispatch_key=key)
        ),
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


def test_a_blocked_fence_write_answers_the_caller_frees_the_slot_and_a_peer_completes(
    rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, pool = rig
    release = threading.Event()
    entered = threading.Event()

    def _blocked_write(*_a, **_k):
        entered.set()
        release.wait(30.0)

    monkeypatch.setattr(server.dispatch_fence, "write", _blocked_write)
    gate = ctx._admission_gate()
    out: list = []
    try:
        thread = _call(ctx, "c4-fence-blocked", out, claimed=True)
        assert _wait(entered.is_set, 10.0), "the overdue kill never reached the fence write"
        assert _wait(lambda: bool(out), 10.0), "the caller was held by the blocked fence write"
        thread.join(5.0)
        assert out[0]["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE
        assert gate._heavy_held == 0, "the blocked fence write still holds the slot"

        peer: list = []
        peer_thread = _call(ctx, "c4-peer", peer, claimed=False)
        assert _wait(lambda: len(pool.futures) >= 2, 10.0), "the peer never reached a worker"
        pool.futures[-1].set_result({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})
        peer_thread.join(10.0)
        assert peer and "result" in peer[0], "the peer op did not complete beside the blocked call"
        assert not release.is_set()
    finally:
        release.set()


def test_a_blocked_claim_create_reads_started_and_frees_the_slot(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _pool = rig
    release = threading.Event()
    entered = threading.Event()

    def _blocked_take(*_a, **_k):
        entered.set()
        release.wait(30.0)
        return True

    monkeypatch.setattr(server, "_take_claim", _blocked_take)
    gate = ctx._admission_gate()
    out: list = []
    try:
        thread = _call(ctx, "c4-claim-blocked", out, claimed=False)
        assert _wait(entered.is_set, 10.0), "the withdrawal never reached the claim create"
        assert _wait(lambda: bool(out), 10.0), "the caller was held by the blocked claim create"
        thread.join(5.0)
        assert out[0]["error"]["code"] == server.WARM_DISPATCH_INDETERMINATE, "an unanswered create must read started"
        assert gate._heavy_held == 0
    finally:
        release.set()


def test_an_answering_claim_create_still_withdraws_an_unstarted_op(rig) -> None:
    ctx, _pool = rig
    out: list = []
    thread = _call(ctx, "c4-unstarted", out, claimed=False)
    thread.join(10.0)
    assert out, "the caller was never answered"
    assert out[0]["error"]["code"] != server.WARM_DISPATCH_INDETERMINATE
    assert ctx._admission_gate()._heavy_held == 0
