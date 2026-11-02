"""build_contract derives one record per op from the registration quad."""
from __future__ import annotations

import dataclasses
import json

import pytest

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.contract.op_contract import OpContract, build_contract


def test_ping_contract_matches_quad():
    c = build_contract("ping")
    assert c.name == "ping"
    assert c.op_class == OP_CLASSIFICATION["ping"]
    assert c.timeout_s == ipc._OP_TIMEOUT_OVERRIDES.get("ping", ipc.DISPATCH_TIMEOUT_SECS)
    assert c.module == "coordinator_core.ops.ping"
    assert c.params_schema == {"type": "object"}
    assert c.summary


def test_to_dict_sorted_keys_and_json_safe():
    d = build_contract("ping").to_dict()
    assert list(d) == sorted(d)
    assert {"name", "summary", "op_class", "timeout_s", "module", "params_schema"} <= set(d)
    json.dumps(d)


def test_contract_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        build_contract("ping").name = "x"  # type: ignore[misc]


def test_unmapped_op_raises_keyerror():
    with pytest.raises(KeyError):
        build_contract("no.such.op")


def test_unclassified_op_raises_keyerror(monkeypatch):
    from coordinator_core.contract import op_contract

    monkeypatch.setitem(op_contract.OP_MODULE_MAP, "zz.unclassified", "coordinator_core.ops.ping")
    with pytest.raises(KeyError):
        build_contract("zz.unclassified")


def test_register_op_params_schema_both_forms_not_dispatch_visible():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}

    @ipc.register_op("zz.test_a", params_schema=schema)
    def _a(params, repo_root=None):
        """A."""

    def _b(params, repo_root=None):
        """B."""

    try:
        ipc.register_op("zz.test_b", _b, params_schema=schema)
        ipc.register_op("zz.test_c", _b)
        assert ipc._PARAMS_SCHEMA["zz.test_a"] == schema
        assert ipc._PARAMS_SCHEMA["zz.test_b"] == schema
        assert "zz.test_c" not in ipc._PARAMS_SCHEMA
    finally:
        for n in ("zz.test_a", "zz.test_b", "zz.test_c"):
            ipc._REGISTRY.pop(n, None)
            ipc._PARAMS_SCHEMA.pop(n, None)


@pytest.mark.parametrize("op", ["ping", "push.outstanding"])
def test_contract_ignores_dispatch_timeout_env(monkeypatch, op):
    before = build_contract(op)
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "1")
    assert build_contract(op) == before
    assert before.timeout_s == ipc._OP_TIMEOUT_OVERRIDES.get(op, ipc.DISPATCH_TIMEOUT_SECS)


def test_ceremony_op_never_reads_override_row(monkeypatch):
    op = "close.background"
    assert ipc.is_ceremony_method(op)
    monkeypatch.setitem(ipc._OP_TIMEOUT_OVERRIDES, op, 999.0)
    assert build_contract(op).timeout_s == float(ipc.DISPATCH_TIMEOUT_SECS)


def test_non_ceremony_op_reads_override_row():
    assert not ipc.is_ceremony_method("push.outstanding")
    assert build_contract("push.outstanding").timeout_s == 130.0


def test_contract_unchanged_when_suspended(monkeypatch):
    before = build_contract("ping")
    monkeypatch.setattr(ipc.op_budget_suspension, "is_suspended", lambda name: True)
    assert build_contract("ping") == before
    assert isinstance(before, OpContract)
