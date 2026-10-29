"""
coordinator_core.ops.tests.test_warm_request_status

Coverage for the "warm.request_status" op registration (C4,
docs/plans/2026-09-23-warm-dispatch-reconcile.md):

  - the op is registered (importing the module populates the IPC registry);
  - it is COMPUTE_ONLY in classification.py;
  - its key scope is "none" in op_scopes.py;
  - it is present in OP_MODULE_MAP, pointing at its own module;
  - the registered handler (reached only on the cold/pool path per D5) always
    answers unknowable(no-resident-engine), and never not_received;
  - a missing/blank `key` param is a ValueError (never a silent no-op).

Negative-spec: this suite does NOT exercise `dispatch_ack.AckStore` or the
`_serve_line` poll intercept — those are C3's surface. This handler is, by
contract (§ 9), never reached when a resident engine's store is visible.

Spec backlink: docs/plans/2026-09-23-warm-dispatch-reconcile.md § C4
"""

from __future__ import annotations

import pytest

import coordinator_core.ops.warm_request_status as wrs
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ipc import _REGISTRY


def test_op_is_registered():
    assert "warm.request_status" in _REGISTRY
    assert _REGISTRY["warm.request_status"] is wrs._warm_request_status


def test_classified_compute_only():
    assert OP_CLASSIFICATION["warm.request_status"] is OpClass.COMPUTE_ONLY


def test_key_scope_is_none():
    assert OP_KEY_SCOPE["warm.request_status"] == "none"


def test_registry_map_points_at_own_module():
    assert OP_MODULE_MAP["warm.request_status"] == "coordinator_core.ops.warm_request_status"


def test_handler_always_answers_unknowable_no_resident_engine():
    result = wrs._warm_request_status({"key": "1234-5678"}, repo_root=None)
    assert result["state"] == "unknowable"
    assert result["reason"] == "no-resident-engine"
    assert result["engine_boot_ns"] is None
    assert result["engine_pid"] is None


def test_handler_never_answers_not_received():
    result = wrs._warm_request_status({"key": "1234-5678"}, repo_root=None)
    assert result["state"] != "not_received"


def test_handler_answers_under_process_budget():
    import time

    start = time.process_time()
    wrs._warm_request_status({"key": "1234-5678"}, repo_root=None)
    assert time.process_time() - start < 0.5


def test_poll_after_timeout_answers_from_store_without_waiting_on_the_op():
    """Production poll path: an admitted record past its deadline answers at once,
    while the op's own thread is still running and the store lock is free."""
    import time

    from coordinator_core.warm import dispatch_ack, server

    store = dispatch_ack.AckStore(executing_bound=lambda _m: 0.02)
    key = dispatch_ack.mint_key()
    assert store.admit(key, "ceremony.commit_v2")
    time.sleep(0.03)
    original = server._ack_store
    server._ack_store = store
    try:
        start_wall = time.perf_counter()
        start_cpu = time.process_time()
        answer = server._store_poll({"key": key})
        wall = time.perf_counter() - start_wall
        cpu = time.process_time() - start_cpu
    finally:
        server._ack_store = original
    assert answer["state"] == dispatch_ack.STATE_EXECUTING
    assert answer["overdue"] is True
    assert cpu < 0.5
    assert wall < 0.5


@pytest.mark.parametrize("params", [{}, {"key": ""}, {"key": None}])
def test_missing_or_blank_key_is_value_error(params):
    with pytest.raises(ValueError):
        wrs._warm_request_status(params, repo_root=None)
