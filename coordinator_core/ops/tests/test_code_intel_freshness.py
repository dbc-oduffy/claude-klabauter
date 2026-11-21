"""code_intel.freshness through the real dispatch path, against a synthetic example-retrieval-repo store."""

from __future__ import annotations

from pathlib import Path

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.symbol_knowledge import freshness


def _handler():
    handler = ipc._REGISTRY.get("code_intel.freshness") or ipc._lazy_import_and_lookup("code_intel.freshness")
    assert handler is not None
    return handler


def test_op_without_a_store_is_not_applicable(tmp_path: Path) -> None:
    out = _handler()({}, repo_root=tmp_path)
    assert out["status"] == "not_applicable" and out["store"] == freshness.STORE_REL


def test_op_is_compute_only() -> None:
    assert OP_CLASSIFICATION["code_intel.freshness"] is OpClass.COMPUTE_ONLY
