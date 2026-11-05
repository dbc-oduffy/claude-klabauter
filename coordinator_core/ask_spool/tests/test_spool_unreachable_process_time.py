"""The unreachable-door spool case sits well under the 50ms bar in the batched benchmark run."""

from __future__ import annotations

import os

import pytest

from coordinator_core.benchmarks import ask_spool_write_budget as B

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.skipif(os.name == "nt", reason="the benchmark drives the POSIX door"),
]

#: Half the bar: the unreachable path is a failed connect plus one O_EXCL write, never a cold-guard spawn.
CEILING_MS = B.BAR_MS / 2


@pytest.fixture(scope="module")
def report() -> dict:
    return B.measure(runs=3)


def test_unreachable_case_spools_one_entry_well_under_the_bar(report):
    res = report["unreachable"]
    assert B.case_passes(res), res
    assert res["process_ms"] < CEILING_MS, res
    assert res["max_ms"] < B.BAR_MS, res


def test_the_bar_is_not_widened():
    assert B.BAR_MS == 50.0
