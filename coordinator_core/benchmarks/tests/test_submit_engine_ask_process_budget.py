"""The submit-path benchmark drives the default `evaluate` and reports process ms, spawns and phases."""

from __future__ import annotations

import types

import pytest

from coordinator_core.benchmarks import submit_engine_ask_process_budget as B


@pytest.fixture(scope="module")
def report() -> dict:
    return B.measure(runs=1)


def test_every_shape_reaches_the_emit_and_allows(report):
    assert set(report) == set(B.SHAPES)
    for shape, res in report.items():
        assert res["decision"] == "allow", (shape, res["reason"])
        assert res["reached_emit_or_launch"], shape


def test_report_carries_process_ms_spawns_and_phase_split(report):
    sizing = report["sizing"]
    for key in ("cold_process_ms", "warm_process_ms", "spawns", "phases_ms", "other_ms"):
        assert key in sizing
    assert "emit" in sizing["phases_ms"]
    assert sizing["warm_process_ms"] >= 0.0
    assert "premint" in report["ask"]["phases_ms"]


def test_a_phase_function_the_hook_lacks_is_skipped_not_fatal():
    hook = types.SimpleNamespace(**{B.PHASES["emit"]: lambda: "emitted"})
    totals: dict = {}
    mp = pytest.MonkeyPatch()
    try:
        with B._timed_phases(mp, hook, totals):
            assert hook._default_emit() == "emitted"
    finally:
        mp.undo()
    assert set(totals) == {"emit"}
