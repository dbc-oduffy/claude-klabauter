"""
coordinator_core.tests.test_dispatch_abandon_deadline

Purpose: `dispatch_abandon.remaining()` hands a handler the dispatcher's own deadline, bound and
reset beside `ACTIVE` in `ipc._dispatch_message_impl`.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import coordinator_core.ipc as ipc
from coordinator_core import dispatch_abandon


@pytest.fixture(autouse=True)
def _warm_route(monkeypatch):
    """Probe ops are unregistered in the classification table, so `_cold_mutating_op` fails
    closed to True and binds no deadline. These tests exercise the bound-deadline path."""
    monkeypatch.setattr(ipc, "_cold_mutating_op", lambda method: False)


def _dispatch(method: str, mid: int = 1) -> dict:
    return asyncio.run(
        ipc.dispatch_message({"jsonrpc": "2.0", "id": mid, "method": method, "params": {}})
    )


def test_remaining_is_none_outside_any_dispatch():
    assert dispatch_abandon.remaining() is None


def test_sync_handler_sees_a_deadline_within_the_narrowed_budget(monkeypatch):
    seen = {}

    def _probe(params, repo_root=None):
        seen["remaining"] = dispatch_abandon.remaining()
        return {}

    monkeypatch.setitem(ipc._REGISTRY, "test.deadline_probe", _probe)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 5.0)

    reply = _dispatch("test.deadline_probe")

    assert "result" in reply
    assert seen["remaining"] is not None
    assert 0.0 < seen["remaining"] <= 5.0


def test_cold_mutating_sync_handler_sees_no_deadline(monkeypatch):
    seen = {}

    def _probe(params, repo_root=None):
        seen["remaining"] = dispatch_abandon.remaining()
        return {}

    monkeypatch.setitem(ipc._REGISTRY, "test.deadline_cold", _probe)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 5.0)
    monkeypatch.setattr(ipc, "_cold_mutating_op", lambda method: True)

    reply = _dispatch("test.deadline_cold")

    assert "result" in reply
    assert seen["remaining"] is None


def test_coroutine_handler_sees_a_deadline(monkeypatch):
    seen = {}

    async def _probe(params, repo_root=None):
        seen["remaining"] = dispatch_abandon.remaining()
        return {}

    monkeypatch.setitem(ipc._REGISTRY, "test.deadline_probe_async", _probe)
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 5.0)

    _dispatch("test.deadline_probe_async")

    assert seen["remaining"] is not None
    assert 0.0 < seen["remaining"] <= 5.0


def test_deadline_is_unbound_after_the_dispatch_returns(monkeypatch):
    monkeypatch.setitem(ipc._REGISTRY, "test.deadline_noop", lambda params, repo_root=None: {})
    monkeypatch.setattr(ipc, "_timeout_for", lambda method, msg=None: 5.0)

    async def _go():
        await ipc.dispatch_message(
            {"jsonrpc": "2.0", "id": 1, "method": "test.deadline_noop", "params": {}}
        )
        return dispatch_abandon.remaining()

    assert asyncio.run(_go()) is None


def test_overlapping_dispatches_each_see_their_own_deadline(monkeypatch):
    seen = {}
    both_in = threading.Barrier(2, timeout=5)

    def _probe(params, repo_root=None):
        both_in.wait()
        seen[params["name"]] = dispatch_abandon.remaining()
        return {}

    budgets = {"short": 2.0, "long": 50.0}
    monkeypatch.setitem(ipc._REGISTRY, "test.deadline_overlap", _probe)
    monkeypatch.setattr(
        ipc, "_timeout_for", lambda method, msg=None: budgets[msg["params"]["name"]]
    )

    async def _go():
        await asyncio.gather(*(
            ipc.dispatch_message({
                "jsonrpc": "2.0", "id": i, "method": "test.deadline_overlap",
                "params": {"name": name},
            })
            for i, name in enumerate(budgets)
        ))

    asyncio.run(_go())

    assert 0.0 < seen["short"] <= 2.0
    assert 2.0 < seen["long"] <= 50.0


def test_remaining_floors_at_zero_once_the_deadline_has_passed():
    token = dispatch_abandon.DEADLINE.set(-1.0)
    try:
        assert dispatch_abandon.remaining() == 0.0
    finally:
        dispatch_abandon.DEADLINE.reset(token)
