"""Each hook dispatch op stays under the dispatch bar of server CPU and its pinned file-IO bound per call."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.benchmarks import hook_ops_process_time as bench
from coordinator_core.benchmarks.preuse_dispatch_phase_cost import _build_fixture

#: `open`/`os.listdir`/`os.scandir` audit events of one steady call; a call touches state proportional to itself, not the corpus.
IO_OPS_BOUND = 16

#: preuse_write measures 20 steady (base 146): one scandir signature per cached schema dir plus the guard chain's own reads.
_IO_OPS_BOUND_OVERRIDE = {bench.OP_PREUSE_WRITE: 20}

_STEADY_CALLS = 3


def _case(op: str):
    return pytest.param(op, id=op)


@pytest.mark.parametrize("op", [_case(op) for op in bench.DISPATCH_OPS])
def test_a_dispatch_op_call_is_under_the_cpu_bar_with_bounded_file_io(op: str, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    fx = _build_fixture(root)
    bench._write_route_table(root)
    row = bench.measure_op(op, bench._payloads(fx)[op], _STEADY_CALLS)
    assert row["error"] is None, row["error"]
    assert row["server_cpu_ms"] < bench.DISPATCH_BAR_MS
    assert row["io_ops_per_call"] <= _IO_OPS_BOUND_OVERRIDE.get(op, IO_OPS_BOUND)


def test_the_five_dispatch_ops_are_the_ones_measured() -> None:
    assert set(bench.DISPATCH_OPS) == {
        "hooks.preuse_bash_dispatch",
        "hooks.preuse_write_dispatch",
        "hooks.postuse_advisory_dispatch",
        "hooks.preuse_search_dispatch",
        "hooks.guard_editor_python_direct",
    }
    assert set(bench.DISPATCH_OPS) <= set(bench.HOOK_OPS)
