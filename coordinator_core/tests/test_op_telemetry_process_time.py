"""
coordinator_core.tests.test_op_telemetry_process_time — C9 coverage.

Purpose: `elapsed_ms` (coordinator_core.telemetry.op_latency.record_op_latency)
stays wall clock, unit and consumers unchanged. Process time is recorded under
a SEPARATE key (`coordinator_core.ipc.record_op_process_time`, row `kind`
`"process_time"`) rather than a redefinition of `elapsed_ms` in place — this
suite pins that split at the two dispatch sites the row builds it for
(`warm.server._run_dispatch` — process-wide; `warm.server._pool_dispatch_worker`
— per-op, uncontaminated) plus the one-shot CLI path
(`coordinator_core.ipc.dispatch_from_hook`), and that the existing
`kind != "complete"` skip in `telemetry/cost_census.py` and
`telemetry/engine_report.py` excludes these rows from every wall-clock
percentile without any consumer edit.

Spec backlink: state/dispatch-briefs/2026-08-21-the-cli-bootstrap-tax-dies-at-
the-interpreter-floor/C9.md
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import coordinator_core.ipc as ipc


def _fake_common_dir(tmp_path):
    common_dir = tmp_path / ".git"
    common_dir.mkdir(exist_ok=True)
    return common_dir


def _sink(common_dir):
    return common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def _read_entries(sink):
    return [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]


def test_record_op_process_time_shape(tmp_path, monkeypatch):
    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)

    ipc.record_op_process_time(
        op="ping",
        process_ms=12.5,
        measurement_scope=ipc.MEASUREMENT_SCOPE_PER_OP_PROCESS,
        source_path="pool_worker",
        t_start=1000.0,
        repo_root=tmp_path,
        sid="sid-abc",
        corr_id="corr-1",
    )

    entries = _read_entries(_sink(common_dir))
    assert len(entries) == 1
    entry = entries[0]
    assert entry["op"] == "ping"
    assert entry["kind"] == "process_time"
    assert entry["process_ms"] == pytest.approx(12.5)
    assert entry["measurement_scope"] == "per_op_process"
    assert entry["source_path"] == "pool_worker"
    assert entry["pid"] == os.getpid()
    assert entry["sid"] == "sid-abc"
    assert entry["corr_id"] == "corr-1"
    # elapsed_ms is a DIFFERENT key -- process_ms never masquerades as it.
    assert "elapsed_ms" not in entry


def test_record_op_process_time_never_raises_on_unresolvable_repo(tmp_path, monkeypatch):
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory", encoding="utf-8")
    unwritable_common_dir = blocking_file / "impossible-child"
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir",
        lambda repo_root: unwritable_common_dir,
    )

    ipc.record_op_process_time(
        op="ping",
        process_ms=1.0,
        measurement_scope=ipc.MEASUREMENT_SCOPE_PROCESS_WIDE,
        source_path="accept_thread",
        t_start=1.0,
        repo_root=tmp_path,
    )


def test_record_op_process_time_no_repo_root_is_a_noop(tmp_path):
    # Must not raise even without a monkeypatched git_common_dir -- repo_root
    # falls back to Path.cwd() per _write_entry's own contract.
    ipc.record_op_process_time(
        op="ping",
        process_ms=1.0,
        measurement_scope=ipc.MEASUREMENT_SCOPE_PER_OP_PROCESS,
        source_path="one_shot_cli",
        t_start=1.0,
        repo_root=None,
    )


def test_unrecognised_measurement_scope_coerced_not_raised(tmp_path, monkeypatch):
    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)

    ipc.record_op_process_time(
        op="ping",
        process_ms=1.0,
        measurement_scope="not-a-real-scope",
        source_path="pool_worker",
        t_start=1.0,
        repo_root=tmp_path,
    )

    entries = _read_entries(_sink(common_dir))
    assert entries[0]["measurement_scope"] == "unknown"


def test_process_time_rows_excluded_from_cost_census_hot_path_percentiles(tmp_path, monkeypatch):
    """A `kind: "process_time"` row must never enter `cost_census`'s
    `elapsed_ms_by_op` population -- it lacks `elapsed_ms` entirely and its
    `kind` fails the module's existing `kind != "complete"` skip, so no
    consumer-side change was needed (verified against source before this row
    was written, per the C9 brief's own instruction)."""
    import time as _time

    from coordinator_core.telemetry import cost_census
    from coordinator_core.telemetry.op_latency import record_op_latency

    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)
    monkeypatch.setattr(cost_census, "HOT_PATH_OPS", ("ceremony.wsc_tail",))

    now = _time.time()
    record_op_latency(
        op="ceremony.wsc_tail", t_start=now, elapsed_ms=42.0, outcome="ok", repo_root=tmp_path,
    )
    ipc.record_op_process_time(
        op="ceremony.wsc_tail",
        process_ms=9999.0,
        measurement_scope=ipc.MEASUREMENT_SCOPE_PER_OP_PROCESS,
        source_path="pool_worker",
        t_start=now,
        repo_root=tmp_path,
    )

    report = cost_census.run_census(repo_root=tmp_path, now=now, lookback_secs=3600.0, write=False)
    summary = report["hot_path_ops"]["ceremony.wsc_tail"]
    # Only the wall-clock elapsed_ms=42.0 row contributes -- the 9999.0
    # process_ms row must never appear as if it were an elapsed_ms sample.
    assert summary["max_ms"] == pytest.approx(42.0)
    assert summary["n"] == 1


def test_pool_dispatch_worker_records_per_op_process_time(tmp_path, monkeypatch):
    """`_pool_dispatch_worker` runs `asyncio.run(dispatch_message(...))`
    entirely inside its own process -- the C9 row's per-op-CPU claim for the
    pool-worker path. Asserts the emitted row's scope and source_path,
    bypassing the real ProcessPoolExecutor (this test runs the target
    function directly, in-process, per the module's own picklable-target
    docstring — the CPU-isolation property is architectural, not something a
    unit test re-proves)."""
    from coordinator_core.warm import server as warm_server

    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)

    async def _fake_dispatch_message(msg):
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    monkeypatch.setattr(ipc, "dispatch_message", _fake_dispatch_message)

    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {},
        "_origin_worktree": str(tmp_path),
    }
    warm_server._pool_dispatch_worker(msg, None)

    entries = [e for e in _read_entries(_sink(common_dir)) if e.get("kind") == "process_time"]
    assert len(entries) == 1
    assert entries[0]["op"] == "ping"
    assert entries[0]["measurement_scope"] == "per_op_process"
    assert entries[0]["source_path"] == "pool_worker"
    assert entries[0]["process_ms"] >= 0.0


def test_run_dispatch_records_process_wide_process_time(tmp_path, monkeypatch):
    """`_run_dispatch` (the accept-process connection-thread path) must
    label its process-time row `MEASUREMENT_SCOPE_PROCESS_WIDE`, never
    `PER_OP_PROCESS` — `WORKER_POOL_SIZE` threads there share one
    interpreter and one `time.process_time()` clock, so the delta can
    include a sibling thread's CPU."""
    from coordinator_core.warm import server as warm_server

    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)

    async def _fake_dispatch_message(msg):
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}

    monkeypatch.setattr(ipc, "dispatch_message", _fake_dispatch_message)

    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {},
        "_origin_worktree": str(tmp_path),
    }
    warm_server._run_dispatch(msg, session_id=None)

    entries = [e for e in _read_entries(_sink(common_dir)) if e.get("kind") == "process_time"]
    assert len(entries) == 1
    assert entries[0]["measurement_scope"] == "process_wide"
    assert entries[0]["source_path"] == "accept_thread"


def test_dispatch_from_hook_records_per_op_process_time_one_shot_cli(tmp_path, monkeypatch):
    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)

    async def _fake_dispatch_message(msg):
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": True}}

    monkeypatch.setattr(ipc, "dispatch_message", _fake_dispatch_message)

    ipc.dispatch_from_hook("ping", {}, origin_worktree=str(tmp_path))

    entries = [e for e in _read_entries(_sink(common_dir)) if e.get("kind") == "process_time"]
    assert len(entries) == 1
    assert entries[0]["op"] == "ping"
    assert entries[0]["measurement_scope"] == "per_op_process"
    assert entries[0]["source_path"] == "one_shot_cli"
