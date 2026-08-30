"""Tests for `coordinator_core.baton_assemble.ops` — the registered
`baton_assemble.brief` (read-only) and `baton_assemble.apply` (MUTATING)
ops, making `baton_assemble` reachable through the warm engine.

Before this module, `baton_assemble` registered no op at all — the only
reachable door was the COLD `entry_point_shim.py` "baton-assemble" forwarder.
The load-bearing assertion here is `get_op_handler` resolving both keys — the
same "unrecognized op in a fresh process" failure mode this chunk's own brief
names as a real prior instance of the four-registry gap.
"""

from __future__ import annotations

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.ipc import get_op_handler
from coordinator_core.op_scopes import OP_KEY_SCOPE
from coordinator_core.ops import _registry_map


def test_baton_assemble_brief_resolves_via_get_op_handler() -> None:
    handler = get_op_handler("baton_assemble.brief")
    assert handler is not None
    assert handler.__name__ == "_baton_assemble_brief"


def test_baton_assemble_apply_resolves_via_get_op_handler() -> None:
    handler = get_op_handler("baton_assemble.apply")
    assert handler is not None
    assert handler.__name__ == "_baton_assemble_apply"


def test_baton_assemble_brief_is_compute_only() -> None:
    assert classify("baton_assemble.brief") is OpClass.COMPUTE_ONLY


def test_baton_assemble_apply_is_mutating() -> None:
    assert classify("baton_assemble.apply") is OpClass.MUTATING


def test_both_ops_scope_to_show_top() -> None:
    assert OP_KEY_SCOPE["baton_assemble.brief"] == "show_top"
    assert OP_KEY_SCOPE["baton_assemble.apply"] == "show_top"


def test_registry_map_names_the_ops_module() -> None:
    assert _registry_map.OP_MODULE_MAP["baton_assemble.brief"] == "coordinator_core.baton_assemble.ops"
    assert _registry_map.OP_MODULE_MAP["baton_assemble.apply"] == "coordinator_core.baton_assemble.ops"
