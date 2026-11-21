"""`dispatch.land_blobs` through its registered handler: declared blobs land in process with no git spawn, and a
hunk outside the run's recorded set refuses the landing naming the foreign run."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import land_blobs_contract as contract


X = "pkg/shared.py"
OWN_RUN = "ask-20261114T100000-aaaaaa"
PEER_RUN = "ask-20261114T100001-bbbbbb"
LINES = 120


def test_the_op_is_registered_and_scoped_under_the_temporary_name():
    from coordinator_core.op_scopes import OP_KEY_SCOPE
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP[contract.TEMP_OP_NAME] == "coordinator_core.ops.dispatch_emit.land_blobs"
    assert OP_KEY_SCOPE[contract.TEMP_OP_NAME] == "common_dir"
    assert contract.ENTRY == "coordinator_core.ops.dispatch_emit.land_blobs:land"
