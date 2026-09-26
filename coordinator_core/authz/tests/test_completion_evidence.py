"""
coordinator_core.authz.tests.test_completion_evidence — guard for the sparse
completion-evidence registry (docs/decisions/DR-442-completion-evidence-is-
the-engine-s-record-not-the-side-effect.md).

Purpose: pin the registry's three invariants -- every key is classified
MUTATING, no entry restates the `ack` default, and the module stays inside
the import ceiling the C1 spike measured -- and pin the fail-closed lookup.

Test convention: pytest. Invoke via
``pytest coordinator_core/authz/tests/test_completion_evidence.py -v``
"""

from __future__ import annotations

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.authz.completion_evidence import (
    OP_COMPLETION_EVIDENCE,
    EvidenceClass,
    evidence_class,
)


def test_every_declared_key_is_classified_mutating() -> None:
    for op in OP_COMPLETION_EVIDENCE:
        assert classify(op) is OpClass.MUTATING, op


def test_no_entry_declares_ack() -> None:
    for op, value in OP_COMPLETION_EVIDENCE.items():
        assert value in (EvidenceClass.FIRE_AND_FORGET,), op


def test_module_has_no_imports_beyond_enum_and_types() -> None:
    import ast
    import inspect

    import coordinator_core.authz.completion_evidence as mod

    tree = ast.parse(inspect.getsource(mod))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    assert names <= {"enum", "types", "__future__"}, names


def test_evidence_class_declared_ops() -> None:
    assert evidence_class("push.outstanding") is EvidenceClass.FIRE_AND_FORGET
    assert evidence_class("app_session.launch") is EvidenceClass.FIRE_AND_FORGET
    assert evidence_class("app_session.teardown") is EvidenceClass.FIRE_AND_FORGET


def test_evidence_class_undeclared_op_is_fail_closed() -> None:
    assert evidence_class("queue.append") is EvidenceClass.UNDECLARED
    assert evidence_class("some.made.up.op.that.does.not.exist") is EvidenceClass.UNDECLARED


def test_evidence_class_handles_unhashable_input() -> None:
    assert evidence_class(["not", "hashable"]) is EvidenceClass.UNDECLARED
    assert evidence_class(None) is EvidenceClass.UNDECLARED
