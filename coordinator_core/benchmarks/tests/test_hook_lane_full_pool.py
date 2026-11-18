"""The hook-lane falsifier: pure verdict logic, plus one real run over real pools that must leave nothing behind."""

from __future__ import annotations

import multiprocessing
import os

from coordinator_core.benchmarks import hook_lane_full_pool as bench


def _op_row(**over) -> dict:
    row = {
        "calls": 20,
        "outcomes": {"ok": 20},
        "p50_reply_ms": 5.0,
        "p95_reply_ms": 40.0,
        "max_reply_ms": 50.0,
        "worker_process_ms_median": 10.0,
        "worker_process_ms_max": 15.0,
        "worker_spawns": 0,
        "accept_process_ms_median": 1.0,
    }
    return {**row, **over}


def _report(op_row: dict, caller: dict, skipped: int = 0) -> dict:
    return {
        "hooks": {"ops": {"hooks.x": op_row}, "skipped_calls": skipped},
        "recycle": {"holders": 1, "unanswered": 0 if caller["answered"] else 1, "callers": [caller]},
    }


_GOOD_CALLER = {"answered": True, "outcome": "error:-32004", "names_op": True, "names_stall_dump": True}


def test_percentile_is_nearest_rank_and_empty_is_none() -> None:
    assert bench.percentile([], 0.95) is None
    assert bench.percentile(list(range(1, 21)), 0.95) == 19
    assert bench.percentile([7.0], 0.95) == 7.0


def test_outcome_names_result_error_code_and_non_reply() -> None:
    assert bench._outcome({"result": {}}) == "ok"
    assert bench._outcome({"error": {"code": -32004}}) == "error:-32004"
    assert bench._outcome(None) == "no-reply"
    assert bench._outcome({"raised": "x"}) == "malformed"


def test_verdict_holds_when_every_clause_is_met() -> None:
    assert bench.verdict(_report(_op_row(), _GOOD_CALLER)) == []


def test_verdict_refuses_each_clause_it_pins() -> None:
    assert any("p95" in r for r in bench.verdict(_report(_op_row(p95_reply_ms=500.0), _GOOD_CALLER)))
    assert any("process" in r for r in bench.verdict(_report(_op_row(worker_process_ms_median=200.0), _GOOD_CALLER)))
    assert any("outcomes" in r for r in bench.verdict(_report(_op_row(outcomes={"ok": 19, "error:-32004": 1}), _GOOD_CALLER)))
    assert any("skipped" in r for r in bench.verdict(_report(_op_row(), _GOOD_CALLER, skipped=3)))
    assert any("no timed call" in r for r in bench.verdict(_report(_op_row(calls=0), _GOOD_CALLER)))
    unanswered = {**_GOOD_CALLER, "answered": False}
    assert any("unanswered" in r for r in bench.verdict(_report(_op_row(), unanswered)))
    no_dump = {**_GOOD_CALLER, "names_stall_dump": False}
    assert any("names_stall_dump=False" in r for r in bench.verdict(_report(_op_row(), no_dump)))
    no_op = {**_GOOD_CALLER, "names_op": False}
    assert any("names_op=False" in r for r in bench.verdict(_report(_op_row(), no_op)))


def test_real_run_answers_every_hook_and_recycle_caller_and_leaves_no_worker_or_env() -> None:
    home_before = os.environ.get("COORDINATOR_SETTINGS_HOME")
    report = bench.run(heavy_holders=2, hook_calls=2, warmup_calls=0, budget_secs=60.0)

    assert report["heavy_holders"] == 2
    ops = report["hooks"]["ops"]
    assert set(ops) == {
        "hooks.preuse_bash_dispatch",
        "hooks.preuse_write_dispatch",
        "hooks.guard_editor_python_direct",
    }
    for op, row in ops.items():
        assert row["calls"] == 2 and row["outcomes"] == {"ok": 2}, (op, row)
        assert row["p95_reply_ms"] is not None and row["worker_process_ms_median"] is not None
    assert report["hooks"]["accept_spawns"] == 0
    assert report["recycle"]["unanswered"] == 0
    assert all(c["answered"] and c["names_op"] for c in report["recycle"]["callers"])
    assert report["recycle"]["recycle_line_files"]
    assert "falsifier:" in bench.format_report(report)

    assert os.environ.get("COORDINATOR_SETTINGS_HOME") == home_before
    assert not os.environ.get(bench.BENCH_DIR_ENV)
    assert multiprocessing.active_children() == []
