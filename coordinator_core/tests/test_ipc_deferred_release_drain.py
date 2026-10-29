"""ipc.dispatch_message drains the session's deferred claim release before an op body."""

from __future__ import annotations

import asyncio

import coordinator_core.ipc as ipc
from coordinator_core.ops.dispatch_emit import land_deadline
from coordinator_core.session import deferred_release


def _patch(monkeypatch, pending: bool):
    calls = []
    monkeypatch.setattr(ipc, "_telemetry_sid", lambda: "sid-1")
    monkeypatch.setattr(deferred_release, "has_pending", lambda s, c: pending)
    monkeypatch.setattr(deferred_release, "drain", lambda s, c: calls.append((s, c)) or [])
    return calls


def _dispatch(tmp_path):
    ipc.register_op("test.drain_probe")(lambda params, repo_root=None: {"ok": True})
    msg = {"jsonrpc": "2.0", "id": 1, "method": "test.drain_probe", "params": {},
           "_origin_worktree": str(tmp_path)}
    return asyncio.run(ipc._dispatch_message_impl(msg))


def test_next_op_drains_pending_record(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, True)
    assert _dispatch(tmp_path)["result"] == {"ok": True}
    assert len(calls) == 1 and calls[0][0] == "sid-1"


def test_no_pending_record_drains_nothing(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, False)
    assert _dispatch(tmp_path)["result"] == {"ok": True}
    assert calls == []


def test_bound_land_deadline_skips_drain(monkeypatch, tmp_path):
    calls = _patch(monkeypatch, True)
    with land_deadline.scope(60):
        assert _dispatch(tmp_path)["result"] == {"ok": True}
    assert calls == []


def test_drain_failure_never_fails_the_op(monkeypatch, tmp_path):
    _patch(monkeypatch, True)

    def boom(s, c):
        raise RuntimeError("x")

    monkeypatch.setattr(deferred_release, "drain", boom)
    assert _dispatch(tmp_path)["result"] == {"ok": True}
