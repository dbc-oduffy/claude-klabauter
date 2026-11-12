"""The submit-path benchmark drives the default `evaluate` and reports process ms, spawns and phases."""

from __future__ import annotations

import types

import pytest

from coordinator_core.benchmarks import fixture_settings_home
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


def test_cold_call_splits_into_legs_and_names_the_largest(report):
    sizing = report["sizing"]
    for key in ("cold_phases_ms", "cold_imports_ms", "cold_largest_leg", "cold_spawns"):
        assert key in sizing
    assert {"compose", "contract_checks"} <= set(B.EXTRA_PHASES)
    assert sizing["cold_largest_leg"] not in B._UMBRELLA


def test_asks_flag_reports_process_ms_spawns_and_counted_durable_writes():
    res = B.measure(runs=1, asks=3)["asks:3"]
    assert res["decision"] == "allow", res["reason"]
    for key in ("process_ms", "spawns", "durable_writes", "writes_by_primitive"):
        assert key in res
    assert res["durable_writes"] == sum(res["writes_by_primitive"].values()) >= 1
    assert "asks:3" not in B.SHAPES


#: Raw-ask premint leg's spawn ceiling: baseline.json (docs/research/emit-outran-hook-budget/2026-11-12-emit-budget/outputs/baseline.json), `ask` shape, spawns 0.
RAW_ASK_SPAWN_CEILING = 0


def test_raw_ask_premint_leg_is_premint_deferred_under_the_bar_and_the_spawn_ceiling(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    B.build_fixture(repo)
    with fixture_settings_home(tmp_path):
        res = B.measure_shape(repo, "ask", runs=3)
    assert res["decision"] == "allow", res["reason"]
    assert res["envelope"] == B.ENVELOPE_PREMINT_DEFERRED, res["reason"]
    assert res["warm_process_ms"] < B.BAR_MS
    assert res["spawns"] <= RAW_ASK_SPAWN_CEILING


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
