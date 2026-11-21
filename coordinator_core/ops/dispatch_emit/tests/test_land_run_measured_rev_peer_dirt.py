"""A shared registration file carrying a peer run's hunks at measure time judges by the run's own hunks."""
from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git as _git_out, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401

pytestmark = pytest.mark.complete

REG = "coordinator_core/ops/_registry_map.py"
SEED = "".join(f"line{i}\n" for i in range(1, 13))
PEER_AT_BASELINE = SEED.replace("line2\n", "peer-two\n")


def test_measured_own_blobs_keeps_only_string_entries_inside_the_commit_set() -> None:
    record = {"own_blobs": {"a": "1" * 40, "b": "2" * 40, "c": 7, "d": ""}}
    assert land_run._measured_own_blobs(record, ["a", "c", "d"]) == {"a": "1" * 40}
    assert land_run._measured_own_blobs({}, ["a"]) == {}
    assert land_run._measured_own_blobs({"own_blobs": ["a"]}, ["a"]) == {}
