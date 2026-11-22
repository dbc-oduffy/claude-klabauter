"""The four rulings.* ops are registered on every surface and their contracts build.

DoE binds to the op names and the reply shapes named in each contract description.
"""
from __future__ import annotations

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.contract.op_contract import build_contract
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.ops._registry_map import OP_MODULE_MAP

OPS = ("rulings.index", "rulings.search", "rulings.gate", "rulings.coverage")
_REPLY_TOKENS = {
    "rulings.index": ("rulings", "applied", "refused"),
    "rulings.search": ("matches",),
    "rulings.gate": ("GateReply", "coordinator:apm"),
    "rulings.coverage": ("covered", "prose_only", "dangling"),
}


@pytest.mark.parametrize("op", OPS)
def test_contract_builds_with_params_and_reply(op):
    c = build_contract(op)
    assert c.name == op
    assert c.module == OP_MODULE_MAP[op]
    assert isinstance(c.params_schema, dict)
    for token in _REPLY_TOKENS[op]:
        assert token in c.description, f"{op}: reply shape token {token!r} missing"


def test_classes_and_scopes_pin_both_sides():
    assert OP_CLASSIFICATION["rulings.index"] is OpClass.MUTATING
    for op in ("rulings.search", "rulings.gate", "rulings.coverage"):
        assert OP_CLASSIFICATION[op] is OpClass.COMPUTE_ONLY
    for op in OPS:
        assert _OP_KEY_SCOPE[op] == "common_dir"


def test_gate_schema_rejects_unknown_keys():
    schema = build_contract("rulings.gate").params_schema
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"body", "path"}
