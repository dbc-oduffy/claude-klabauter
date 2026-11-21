"""Pins for the update-docs / distill process-time benchmark's helpers and verdict."""

from __future__ import annotations

import subprocess
import sys

import pytest

from coordinator_core.benchmarks import update_docs_distill_process_time as bench

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_measure_op_counts_spawns_bytes_and_worklist() -> None:
    def call() -> dict[str, list[int]]:
        subprocess.run([sys.executable, "-c", "pass"], check=True)
        return {"worklist": [1, 2, 3]}

    row = bench.measure_op("fake.op", call, bench.update_docs_worklist_len, "rag_off", runs=2)
    assert row["op_name"] == "fake.op"
    assert len(row["runs"]) == 2
    assert row["spawns"] == 1
    assert row["worklist_len"] == 3
    assert row["output_bytes"] == len('{"worklist": [1, 2, 3]}')
    assert row["process_ms"] >= 0
