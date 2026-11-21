"""code_intel.workstate_index through the real dispatch path, against a fixture repo."""

from __future__ import annotations


from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass


def test_op_is_classified_mutating_and_scoped() -> None:
    assert OP_CLASSIFICATION["code_intel.workstate_index"] is OpClass.MUTATING
    assert ipc.OP_KEY_SCOPE.get("code_intel.workstate_index") == "show_top"
