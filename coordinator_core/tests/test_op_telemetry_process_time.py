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
        caller="coordinator_core.ops.check_auto_reconcile",
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
    assert entry["caller"] == "coordinator_core.ops.check_auto_reconcile"
    # elapsed_ms is a DIFFERENT key -- process_ms never masquerades as it.
    assert "elapsed_ms" not in entry


def test_record_op_process_time_caller_defaults_to_none(tmp_path, monkeypatch):
    """A caller that omits `caller=` (pre-C16) gets `caller: null`, never a
    raise -- purely additive, same contract as `record_op_latency`'s own
    optional `caller` field."""
    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)

    ipc.record_op_process_time(
        op="ping",
        process_ms=1.0,
        measurement_scope=ipc.MEASUREMENT_SCOPE_PER_OP_PROCESS,
        source_path="pool_worker",
        t_start=1.0,
        repo_root=tmp_path,
    )

    entries = _read_entries(_sink(common_dir))
    assert entries[0]["caller"] is None


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

    captured_caller = {}

    async def _fake_dispatch_message(msg, *, caller=None):
        captured_caller["caller"] = caller
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
    assert entries[0]["caller"] == "coordinator_core.warm.server._pool_dispatch_worker"
    assert captured_caller["caller"] == "coordinator_core.warm.server._pool_dispatch_worker"


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

    captured_caller = {}

    async def _fake_dispatch_message(msg, *, caller=None):
        captured_caller["caller"] = caller
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
    assert entries[0]["caller"] == "coordinator_core.warm.server._run_dispatch"
    assert captured_caller["caller"] == "coordinator_core.warm.server._run_dispatch"


def test_dispatch_from_hook_records_per_op_process_time_one_shot_cli(tmp_path, monkeypatch):
    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)

    captured_caller = {}

    async def _fake_dispatch_message(msg, *, caller=None):
        captured_caller["caller"] = caller
        return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"ok": True}}

    monkeypatch.setattr(ipc, "dispatch_message", _fake_dispatch_message)

    ipc.dispatch_from_hook("ping", {}, origin_worktree=str(tmp_path))

    entries = [e for e in _read_entries(_sink(common_dir)) if e.get("kind") == "process_time"]
    assert len(entries) == 1
    assert entries[0]["op"] == "ping"
    assert entries[0]["measurement_scope"] == "per_op_process"
    assert entries[0]["source_path"] == "one_shot_cli"
    assert entries[0]["caller"] == "coordinator_core.ipc.dispatch_from_hook"
    assert captured_caller["caller"] == "coordinator_core.ipc.dispatch_from_hook"

def test_process_time_rows_carry_a_session_id(tmp_path, monkeypatch):
    """A process-time row must be joinable to the session that produced it.

    Regression, 2026-08-25. Every `process_time`-kind row in the live sink
    carried `sid: null` -- 2,108 of 2,108 for `hooks.track_touched_files` --
    because none of the four `record_op_process_time` call sites passed one,
    while the wall-clock rows beside them did. A CPU sample that cannot be
    joined to a session cannot be ranked within one, so the sink could not
    answer "what does fire #1 of a session cost" in process time at all, and
    an audit that needed exactly that had to fall back to wall clock.
    """
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-under-test")
    from coordinator_core.ipc import _telemetry_sid

    assert _telemetry_sid() == "sid-under-test"


def test_telemetry_sid_never_raises(monkeypatch):
    """Telemetry must never break dispatch -- a null sid beats a raise."""
    import coordinator_core.session.core as _core

    def _boom(*a, **kw):
        raise RuntimeError("resolution exploded")

    monkeypatch.setattr(_core, "resolve_session_id", _boom)
    from coordinator_core.ipc import _telemetry_sid

    assert _telemetry_sid() is None


def test_invoke_cli_dispatch_records_a_process_time_row(tmp_path, monkeypatch):
    """The `coordinator_core.invoke` CLI branch samples process time too.

    Regression, 2026-08-25. Four dispatch sites sampled process time -- two in
    `warm/server.py`, two in `ipc.py` -- and this module's own cold branch,
    which is a fifth, sampled none. Measured over a 24h window of the live
    sink: every `handoff.reconcile_open` and `write_surface.emit_manifest` row
    arrived through here, so the ops furthest over the wall-clock bar were
    precisely the ops carrying no CPU sample at all. A budget stated in process
    time cannot be read against an op that never emits one.
    """
    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-invoke-cli")

    from coordinator_core.invoke.__main__ import _record_dispatch_process_time

    _record_dispatch_process_time(
        {"jsonrpc": "2.0", "method": "ping", "params": {"cwd": str(tmp_path)}},
        1000.0,
        0.0,
    )

    entries = [e for e in _read_entries(_sink(common_dir)) if e.get("kind") == "process_time"]
    assert len(entries) == 1
    assert entries[0]["op"] == "ping"
    assert entries[0]["source_path"] == "invoke_cli"
    # PROCESS_WIDE, never PER_OP_PROCESS: this branch is also reached by
    # `invoke.from_argv` on a REUSED warm-server executor thread, where sibling
    # threads share one `time.process_time()` clock. Under-claiming a sample's
    # scope costs a reader confidence; over-claiming costs them a conclusion.
    assert entries[0]["measurement_scope"] == "process_wide"
    assert entries[0]["sid"] == "sid-invoke-cli"
    assert entries[0]["caller"] == "coordinator_core.invoke.__main__"
    assert "elapsed_ms" not in entries[0]


def test_invoke_cli_process_time_never_breaks_dispatch(tmp_path, monkeypatch):
    """Telemetry failure on the CLI path costs a row, never the envelope."""
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir",
        lambda repo_root: (_ for _ in ()).throw(RuntimeError("no common dir")),
    )

    from coordinator_core.invoke.__main__ import _record_dispatch_process_time

    _record_dispatch_process_time({"method": "ping"}, 1.0, 0.0)
    _record_dispatch_process_time({}, 1.0, 0.0)
    _record_dispatch_process_time(None, 1.0, 0.0)  # type: ignore[arg-type]


def test_invoke_cli_cold_branch_still_calls_the_recorder():
    """Pins the CALL, not just the helper.

    The helper passing its own unit test proves nothing if a future edit to the
    dispatch block drops the call -- which is exactly how this entry point came
    to be the uninstrumented one. Asserted on source text because driving the
    real branch needs a full argv/warm-client bootstrap this suite does not own.
    """
    from pathlib import Path

    import coordinator_core.invoke.__main__ as invoke_main

    source = Path(invoke_main.__file__).read_text(encoding="utf-8")
    finally_block = source.split(
        "response = loop.run_until_complete(dispatch_message(msg, caller=_CALLER))"
    )[1]
    finally_block = finally_block.split("_captured = _handler_stdout.getvalue()")[0]
    assert "_record_dispatch_process_time(" in finally_block
    assert "loop.close()" in finally_block


# --- C1 caller-provenance stamp (2026-08-25-reconcile-open-comes-back-under-the-bar) ---


def test_caller_module_reports_the_calling_test_module():
    """Called directly (no dispatch chokepoint in between), `caller_module()`
    must name THIS test module -- it is not itself skipped, since it is not
    `coordinator_core.ipc`/`coordinator_core.telemetry.op_latency`/`asyncio`."""
    from coordinator_core.telemetry.op_latency import caller_module

    assert caller_module() == __name__


def test_caller_module_skips_the_ipc_dispatch_chokepoint(tmp_path, monkeypatch):
    """A row recorded via `ipc.dispatch_message` must attribute to the module
    that called `dispatch_message`, never to `coordinator_core.ipc` itself --
    the whole point `_CALLER_SKIP_PREFIXES` exists to enforce."""
    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)

    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {},
        "_origin_worktree": str(tmp_path),
    }
    asyncio.run(ipc.dispatch_message(msg))

    entries = _read_entries(_sink(common_dir))
    started = [e for e in entries if e.get("kind") == "started"]
    complete = [e for e in entries if e.get("kind") == "complete"]
    assert len(started) == 1
    assert len(complete) == 1
    # This test module is the true caller of dispatch_message (via
    # asyncio.run, which interposes asyncio.* frames the walk must skip).
    assert started[0]["caller"] == __name__
    assert complete[0]["caller"] == __name__
    # Same corr_id, same caller -- resolved once, not re-walked per row.
    assert started[0]["corr_id"] == complete[0]["corr_id"]


def test_record_op_latency_and_started_carry_an_optional_caller_field(tmp_path, monkeypatch):
    """`caller` is purely additive -- an explicit value round-trips, and
    omitting it (pre-C1 callers) never raises and defaults to null."""
    from coordinator_core.telemetry.op_latency import record_op_latency, record_op_started

    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)

    record_op_started(
        op="ping", t_start=1.0, corr_id="corr-caller-1", repo_root=tmp_path,
        caller="coordinator_core.ops.check_auto_reconcile",
    )
    record_op_latency(
        op="ping", t_start=1.0, elapsed_ms=1.0, outcome="ok", repo_root=tmp_path,
        corr_id="corr-caller-1",
    )

    entries = _read_entries(_sink(common_dir))
    assert entries[0]["caller"] == "coordinator_core.ops.check_auto_reconcile"
    # Omitted on the second call -- defaults to None, never raises.
    assert entries[1]["caller"] is None


def test_caller_module_never_raises_at_the_top_of_the_stack(monkeypatch):
    """Defensive per the module's never-breaks-dispatch contract: a
    `sys._getframe` failure degrades to `None`, never a raised exception."""
    from coordinator_core.telemetry import op_latency

    def _boom(_depth):
        raise ValueError("no frame")

    monkeypatch.setattr(op_latency.sys, "_getframe", _boom)
    assert op_latency.caller_module() is None


# --- C15: attribution by construction, not by stack walk ------------------
# (2026-08-25-reconcile-open-comes-back-under-the-bar)


def test_dispatch_message_explicit_caller_wins_over_the_stack_walk(tmp_path, monkeypatch):
    """A caller that declares itself via `dispatch_message(msg, caller=...)`
    is attributed to THAT declared string, never to the frame-walk's answer
    (which would otherwise resolve to this test module, per the test above)."""
    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)

    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {},
        "_origin_worktree": str(tmp_path),
    }
    asyncio.run(ipc.dispatch_message(msg, caller="coordinator_core.ops.check_auto_reconcile"))

    entries = _read_entries(_sink(common_dir))
    started = [e for e in entries if e.get("kind") == "started"]
    complete = [e for e in entries if e.get("kind") == "complete"]
    assert started[0]["caller"] == "coordinator_core.ops.check_auto_reconcile"
    assert complete[0]["caller"] == "coordinator_core.ops.check_auto_reconcile"
    # Declared identity is real caller-asserted attribution, not `cwd`-style
    # inference -- the row must never read the useless stack-walk fallback.
    assert started[0]["caller"] != __name__


def test_dispatch_message_falls_back_to_the_walk_when_no_caller_declared(tmp_path, monkeypatch):
    """A caller that omits `caller=` (not yet migrated to declare itself)
    still gets SOME attribution via the retained `caller_module()` fallback
    -- the walk is dead code on the measured, migrated population, not a
    removed capability for an undeclared one."""
    common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir)
    monkeypatch.setattr(ipc, "_STAMP_GATE_ARMED", False)

    msg = {
        "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {},
        "_origin_worktree": str(tmp_path),
    }
    asyncio.run(ipc.dispatch_message(msg))

    entries = _read_entries(_sink(common_dir))
    started = [e for e in entries if e.get("kind") == "started"]
    assert started[0]["caller"] == __name__


def test_caller_module_prefers_spec_name_over_dunder_name_for_a_main_frame():
    """The measured C15 finding: a module executed as the process entry point
    (`python -m ...`) carries `__name__ == "__main__"` in its OWN frame --
    a correct answer to "which module" and a useless one to "which call
    site". `__spec__.name` carries the real dotted name for exactly this
    case (import machinery sets it identically for `-m` execution and normal
    import); `caller_module()` must prefer it over the dunder when present."""
    from coordinator_core.telemetry.op_latency import caller_module

    class _FakeSpec:
        name = "coordinator_core.invoke.__main__"

    class _FakeFrame:
        def __init__(self, f_globals, f_back):
            self.f_globals = f_globals
            self.f_back = f_back

    caller_frame = _FakeFrame({"__name__": "__main__", "__spec__": _FakeSpec()}, None)
    entry_frame = _FakeFrame({"__name__": "coordinator_core.ipc"}, caller_frame)

    import coordinator_core.telemetry.op_latency as op_latency_mod

    original_getframe = op_latency_mod.sys._getframe
    try:
        op_latency_mod.sys._getframe = lambda depth: entry_frame
        assert caller_module() == "coordinator_core.invoke.__main__"
    finally:
        op_latency_mod.sys._getframe = original_getframe


def test_caller_module_falls_back_to_dunder_name_when_spec_is_absent():
    """A frame with no `__spec__` at all (e.g. code executed via `exec`, or
    an older/synthetic frame) must still resolve via the plain `__name__`
    lookup -- the `__spec__` preference is a strict widening, not a
    narrowing of what the walk could already resolve."""
    from coordinator_core.telemetry.op_latency import caller_module

    class _FakeFrame:
        def __init__(self, f_globals, f_back):
            self.f_globals = f_globals
            self.f_back = f_back

    caller_frame = _FakeFrame({"__name__": "coordinator_core.ops.check_auto_reconcile"}, None)
    entry_frame = _FakeFrame({"__name__": "coordinator_core.ipc"}, caller_frame)

    import coordinator_core.telemetry.op_latency as op_latency_mod

    original_getframe = op_latency_mod.sys._getframe
    try:
        op_latency_mod.sys._getframe = lambda depth: entry_frame
        assert caller_module() == "coordinator_core.ops.check_auto_reconcile"
    finally:
        op_latency_mod.sys._getframe = original_getframe
