"""The submit-path legs benchmark records per-leg figures for a refire under concurrent run starts.

The pure pieces (leg split, the largest leg, the instrumentation seam in the live `warm/server.py`) run
everywhere. The end-to-end pin boots a throwaway warm engine and drives the installed `hook-run` door; it
records the figures and asserts their shape; the bar is not enforced here yet.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.benchmarks import submit_ask_hook_to_reply_legs as legs

# Resolved at import: the suite root's conftest quarantines HOME for unmarked tests.
_DOOR = legs.installed_door()


def _stamps(tag: str, *, accept: float, enqueue: float, start: float, dispatch: float, end: float,
            admission_ms: float, pool_ms: float) -> list[dict]:
    return [
        {"kind": "admission", "tag": tag, "wait_ms": admission_ms},
        {"kind": "pool_dispatch", "tag": tag, "ms": pool_ms, "start_ts": dispatch, "error": None},
        {"kind": "served", "tag": tag, "op": "hook-run", "lane": "worker", "accept_ts": accept, "enqueue_ts": enqueue,
         "start_ts": start, "end_ts": end, "busy": 1},
    ]


def test_split_legs_names_each_leg_and_the_largest():
    stamps = _stamps("legs-refire", accept=100.0, enqueue=100.05, start=100.25, dispatch=100.3, end=101.3,
                     admission_ms=300.0, pool_ms=1000.0)
    got = legs.split_legs(1500.0, stamps, "legs-refire", spawn_wall=99.9, first_byte_wall=101.4)
    assert got["connect_ms"] == pytest.approx(100.0, abs=0.2)
    assert got["accept_to_enqueue_ms"] == pytest.approx(50.0, abs=0.2)
    assert got["queue_wait_ms"] == pytest.approx(200.0, abs=0.2)
    assert got["deliver_ms"] == pytest.approx(50.0, abs=0.2)
    assert got["admission_wait_ms"] == 300.0
    assert got["pool_dispatch_ms"] == 700.0, "the pool leg is the dispatch time after the gate wait"
    assert got["reply_ms"] == pytest.approx(100.0, abs=0.2)
    assert got["first_byte_ms"] == pytest.approx(1500.0, abs=0.2)
    assert got["largest_leg"] == "pool_dispatch_ms"


def test_split_legs_a_slow_connect_is_the_largest_leg():
    stamps = _stamps("legs-refire", accept=18.0, enqueue=18.001, start=18.002, dispatch=18.003, end=18.1,
                     admission_ms=0.0, pool_ms=90.0)
    got = legs.split_legs(9000.0, stamps, "legs-refire", spawn_wall=10.0, first_byte_wall=18.2)
    assert got["connect_ms"] == pytest.approx(8000.0, abs=0.2)
    assert got["largest_leg"] == "connect_ms"


def test_split_legs_ignores_another_submits_stamps():
    stamps = _stamps("legs-start-0", accept=1.0, enqueue=1.0, start=1.0, dispatch=1.0, end=2.0,
                     admission_ms=900.0, pool_ms=950.0)
    got = legs.split_legs(50.0, stamps, "legs-refire", spawn_wall=0.5, first_byte_wall=0.55)
    for leg in ("connect_ms", "deliver_ms", "queue_wait_ms", "admission_wait_ms", "pool_dispatch_ms", "reply_ms"):
        assert got[leg] is None, f"{leg} read another submit's stamp"
    assert got["largest_leg"] is None and got["door_total_ms"] == 50.0 and got["first_byte_ms"] == pytest.approx(50.0, abs=0.2)


def test_the_synthetic_load_runs_its_workers_and_leaves_none_behind(tmp_path):
    import psutil

    with legs.synthetic_load(2, tmp_path):
        children = psutil.Process().children(recursive=True)
        assert sum("python" in c.name().lower() for c in children) >= 2
    survivors = []
    for child in children:
        try:
            if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                survivors.append(child)
        except psutil.NoSuchProcess:
            continue
    assert not survivors


def test_read_stamps_drops_a_torn_last_line(tmp_path):
    path = tmp_path / "stamps.jsonl"
    path.write_text('{"kind": "served", "tag": "a"}\n{"kind": "adm', encoding="utf-8")
    assert legs.read_stamps(path) == [{"kind": "served", "tag": "a"}]
    assert legs.read_stamps(tmp_path / "absent.jsonl") == []


def test_instrumentation_seam_exists_in_the_live_server_source():
    server = Path(__file__).resolve().parents[2] / "warm" / "server.py"
    patched = legs.instrumented_server_source(server.read_text(encoding="utf-8"))
    compile(patched, "server.py", "exec")
    assert patched.rstrip().endswith(legs._SERVER_TAIL.rstrip())
    assert "_AdmissionGate.acquire = _legs_acquire_wrapped" in patched
    for name in ("_ServerContext._pool_dispatch", "_ServerContext._record_served_row", "_AdmissionGate.acquire",
                 "_ServerContext._serve_queued", "_ServerContext._enqueue_connection",
                 "_ServerContext._boot_prewarm_run"):
        assert name.split(".")[1] in server.read_text(encoding="utf-8")
        assert f"{name} = " in patched
    assert "def _connect_pipe(" in server.read_text(encoding="utf-8")
    assert "_connect_pipe = _legs_connect_pipe_wrapped" in patched


def test_instrumentation_refuses_a_server_that_lost_its_main_guard():
    with pytest.raises(ValueError, match="no seam"):
        legs.instrumented_server_source("def main():\n    return 0\n")


#: Spawns the submit's `evaluate` may make under concurrent run starts: it reads and emits in process.
SPAWN_BUDGET = 0


@pytest.fixture(scope="module")
def first_burst_report() -> dict:
    """Round 0 is the first request burst after a fresh engine boot (no warmup round to hide a cold pool)."""
    return legs.measure(concurrent_runs=3, runs=3, warmups=0, door=_DOOR)


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(_DOOR is None, reason="no installed hook-run door on this box")
@pytest.mark.skipif(os.name != "nt", reason="the engine-clone job-object teardown is measured on Windows")
def test_no_spool_and_no_leg_over_the_bar_from_the_first_burst_after_boot(first_burst_report):
    report = first_burst_report
    rounds = report["rounds"]
    assert len(rounds) == 3
    over: list[str] = []
    for n, round_rows in enumerate(rounds):
        assert [row["role"] for row in round_rows] == ["start", "start", "start", "refire"]
        for row in round_rows:
            for key in (*legs.LEGS, "evaluate_process_ms", "spool", "largest_leg"):
                assert key in row
            assert row["engine_span_ms"] is not None, f"the engine never stamped {row['tag']}: {row['reply']}"
            assert not row["spool"], f"round {n} {row['tag']} spooled: {row['reply']}"
            over += [f"round {n} {row['tag']} {leg}={row[leg]}" for leg in (*legs.LEGS, "first_byte_ms")
                     if row[leg] is not None and row[leg] > legs.BAR_MS]
            assert row["evaluate_process_ms"] <= legs.BAR_MS
    assert report["spooled"] is False
    assert report["load_workers"] == legs.DEFAULT_LOAD_WORKERS
    assert not over, f"legs over {legs.BAR_MS}ms (round 0 is the first burst after boot): {over}"
    print("SUBMIT-LEGS", {k: v for k, v in report.items() if k != "rounds"})


def test_the_submit_stays_within_its_process_time_and_spawn_budget_under_three_run_starts(monkeypatch):
    from coordinator_core.benchmarks import submit_engine_ask_process_budget as budget

    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")

    res = budget.measure(runs=1, concurrent_runs=3)["concurrent:3"]
    assert res["spooled"] is False
    assert res["decision"] == "allow", res["reason"]
    assert res["max_process_ms"] <= legs.BAR_MS, res["phases_ms"]
    assert res["spawns"] <= SPAWN_BUDGET, res["spawn_argv"]
