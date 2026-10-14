"""Pins the engine per-op overhead contract: arithmetic, bar reasons, segment order, constant budgets."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core.benchmarks import engine_op_overhead as eoo
from coordinator_core.benchmarks.engine_op_overhead import (
    OVERHEAD_BUDGET_MS,
    SEGMENTS,
    UNEXPLAINED_PROCS_BUDGET,
    OverheadSample,
    in_process_sample_ms,
    over_budget,
)


def _sample(**kw) -> OverheadSample:
    base = dict(probe="p", warm_op_ms=100.0, in_process_ms=60.0, warm_procs=4.0, in_process_procs=3.0)
    base.update(kw)
    return OverheadSample(**base)


def test_overhead_and_unexplained_are_differences():
    s = _sample()
    assert s.overhead_ms == pytest.approx(40.0)
    assert s.unexplained_procs == pytest.approx(1.0)


def test_segments_order():
    assert SEGMENTS == ("dispatch", "job_object", "json", "extra_process", "residual")


def test_budgets_are_the_contract_values():
    assert OVERHEAD_BUDGET_MS == 50.0
    assert UNEXPLAINED_PROCS_BUDGET == 0.0


def test_within_both_bars_is_none():
    assert over_budget(_sample(warm_op_ms=110.0, warm_procs=3.0)) is None


def test_at_the_bar_is_within():
    assert over_budget(_sample(warm_op_ms=60.0 + OVERHEAD_BUDGET_MS, warm_procs=3.0)) is None


def test_time_bar_reason_names_figure_and_bar():
    reason = over_budget(_sample(warm_op_ms=160.0, warm_procs=3.0))
    assert reason is not None
    assert "overhead_ms 100.000" in reason and "50.0ms" in reason and "unexplained" not in reason


def test_procs_bar_reason_names_figure_and_bar():
    reason = over_budget(_sample(warm_procs=3.67))
    assert reason is not None
    assert "unexplained_procs 0.670" in reason and "overhead_ms" not in reason


def test_both_bars_reported_together():
    reason = over_budget(_sample(warm_op_ms=200.0, warm_procs=5.0))
    assert reason is not None and "overhead_ms" in reason and "unexplained_procs" in reason


def test_negative_overhead_is_within():
    assert over_budget(_sample(warm_op_ms=50.0, warm_procs=3.0)) is None


def test_budgets_are_module_constants_not_env_reads():
    src = Path(eoo.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    readers = [
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "os.environ" in (ast.get_source_segment(src, node) or "")
    ]
    assert readers == ["_boot_env"], f"os.environ read outside _boot_env: {readers}"


def test_in_process_sample_refuses_nonpositive_k():
    with pytest.raises(ValueError, match="k must be >= 1"):
        in_process_sample_ms(["py", "-m", "drv"], k=0)


def test_in_process_sample_subtracts_setup_only_run(monkeypatch):
    calls = []

    def fake(cmd, k=20, **_kw):
        calls.append(list(cmd))
        measured = cmd[-1] != "0"
        return {"process_time_ms": 130.0 if measured else 100.0,
                "procs_per_call": 4.0 if measured else 1.0, "rc": 0, "k": k}

    monkeypatch.setattr(eoo, "batched_process_time_ms", fake)
    ms, procs = in_process_sample_ms(["py", "-m", "drv"], k=3)
    assert (ms, procs) == (pytest.approx(10.0), pytest.approx(1.0))
    assert calls[0][-2:] == ["--calls", "3"] and calls[1][-2:] == ["--calls", "0"]


def test_in_process_sample_refuses_failed_driver(monkeypatch):
    monkeypatch.setattr(
        eoo, "batched_process_time_ms",
        lambda cmd, k=20, **_kw: {"process_time_ms": 1.0, "procs_per_call": 1.0, "rc": 2, "k": k},
    )
    with pytest.raises(RuntimeError, match="rc=2"):
        in_process_sample_ms(["py", "-m", "drv"], k=3)
