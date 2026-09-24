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


@pytest.mark.parametrize("params", [{}, {"key": ""}, {"key": None}])
def test_missing_or_blank_key_is_value_error(params):
    with pytest.raises(ValueError):
        wrs._warm_request_status(params, repo_root=None)
