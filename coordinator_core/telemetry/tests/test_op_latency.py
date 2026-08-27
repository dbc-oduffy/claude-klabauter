"""
coordinator_core.telemetry.tests.test_op_latency — op-latency sink coverage.

Purpose: exercises coordinator_core.telemetry.op_latency's record shape,
concurrent-append safety, kill-switch, and fail-open-on-unwritable-sink
guarantees. Real-traffic load-norm measurement instrument coverage — see
op_latency.py's own module docstring for the full negative-spec these tests
assert against.

Spec backlink: state/handoffs/2026-08-08-engine-fails-the-load-norm.md
               docs/wiki/machine-load-norm.md
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.telemetry.op_latency import (
    _ERROR_KIND_MAX_CHARS,
    breach_summary,
    new_correlation_id,
    pairing_summary,
    record_composition_span,
    record_fact_span,
    record_op_latency,
    record_op_started,
    tail_entries,
)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency.

    Same house convention as coordinator_core.tests.test_dispatch_message._run
    (engine is stdlib-only).
    """
    return asyncio.run(coro)


def _child_append(sink_path_str: str, n: int, idx: int) -> None:
    """Multiprocessing worker: append `n` records directly via the sink's
    write primitive (``op_latency._append_line``), bypassing git/repo
    resolution — a fresh spawn-context process re-imports the module and
    cannot see this test's monkeypatched ``git_common_dir``, so exercising
    the concurrency-safety guarantee itself (not the resolution plumbing)
    is what this worker is for.

    Must be a module-level function (not a closure/lambda) so it is
    picklable for spawn-based multiprocessing (the default on Windows).
    """
    import json as _json

    from coordinator_core.telemetry.op_latency import _append_line

    sink = Path(sink_path_str)
    for i in range(n):
        entry = {
            "op": f"test.op.{idx}",
            "t_start": 1000.0 + i,
            "elapsed_ms": 1.5,
            "outcome": "ok",
            "pid": os.getpid(),
            "sid": f"sid-{idx}",
            "repo_key": None,
        }
        line = (_json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
        _append_line(sink, line)


def test_record_shape(tmp_path, monkeypatch):
    fake_common_dir = tmp_path / ".git"
    fake_common_dir.mkdir()

    def _fake_git_common_dir(repo_root):
        return fake_common_dir

    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", _fake_git_common_dir
    )

    record_op_latency(
        op="ping",
        t_start=12345.678,
        elapsed_ms=42.5,
        outcome="ok",
        repo_root=tmp_path,
        sid="sid-abc",
    )

    sink = fake_common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    assert sink.exists()
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["op"] == "ping"
    assert entry["t_start"] == pytest.approx(12345.678)
    assert entry["elapsed_ms"] == pytest.approx(42.5)
    assert entry["outcome"] == "ok"
    assert entry["pid"] == os.getpid()
    assert entry["sid"] == "sid-abc"
    assert entry["repo_key"] == str(fake_common_dir)


def test_kill_switch(tmp_path, monkeypatch):
    fake_common_dir = tmp_path / ".git"
    fake_common_dir.mkdir()
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )
    monkeypatch.setenv("COORDINATOR_OP_LATENCY_DISABLE", "1")

    record_op_latency(
        op="ping", t_start=1.0, elapsed_ms=1.0, outcome="ok", repo_root=tmp_path,
    )

    sink = fake_common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    assert not sink.exists()


def test_unwritable_sink_does_not_raise(tmp_path, monkeypatch):
    # A repo_root whose resolved git_common_dir doesn't exist and can't be
    # created (parent is a FILE, not a directory) forces the os.makedirs /
    # os.open path to fail — record_op_latency must swallow it.
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory", encoding="utf-8")
    unwritable_common_dir = blocking_file / "impossible-child"

    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir",
        lambda repo_root: unwritable_common_dir,
    )

    # Must not raise.
    record_op_latency(
        op="ping", t_start=1.0, elapsed_ms=1.0, outcome="error", repo_root=tmp_path,
    )


def test_no_repo_root_is_a_noop(tmp_path):
    # No monkeypatch needed -- repo_root=None short-circuits before any
    # lifecycle import/resolution is attempted.
    record_op_latency(op="ping", t_start=1.0, elapsed_ms=1.0, outcome="ok", repo_root=None)


def _fake_common_dir(tmp_path):
    fake_common_dir = tmp_path / ".git"
    fake_common_dir.mkdir()
    return fake_common_dir


def _sink_for(fake_common_dir):
    return fake_common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def test_started_row_lands_before_handler_runs(tmp_path, monkeypatch):
    # Simulates "the handler hasn't run yet" by simply never calling
    # record_op_latency — the started row must already be durable on its own.
    fake_common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )

    corr_id = new_correlation_id()
    record_op_started(
        op="ping", t_start=1.0, corr_id=corr_id, repo_root=tmp_path, sid="sid-x",
    )

    sink = _sink_for(fake_common_dir)
    assert sink.exists()
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["kind"] == "started"
    assert entry["corr_id"] == corr_id
    assert entry["op"] == "ping"
    assert entry["sid"] == "sid-x"


def test_started_and_complete_rows_share_corr_id(tmp_path, monkeypatch):
    fake_common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )

    corr_id = new_correlation_id()
    record_op_started(op="ping", t_start=1.0, corr_id=corr_id, repo_root=tmp_path)
    record_op_latency(
        op="ping", t_start=1.0, elapsed_ms=5.0, outcome="ok",
        repo_root=tmp_path, corr_id=corr_id,
    )

    sink = _sink_for(fake_common_dir)
    lines = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["kind"] == "started"
    assert lines[1]["kind"] == "complete"
    assert lines[0]["corr_id"] == lines[1]["corr_id"] == corr_id


def test_mid_flight_death_leaves_one_unpaired_started_row(tmp_path, monkeypatch):
    # Simulated mid-flight death: invoke the started writer and never call
    # the completion writer (a real kill is not exercised in this suite).
    fake_common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )

    corr_id = new_correlation_id()
    record_op_started(op="vanishes", t_start=1.0, corr_id=corr_id, repo_root=tmp_path)

    sink = _sink_for(fake_common_dir)
    lines = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    assert lines[0]["kind"] == "started"
    assert lines[0]["corr_id"] == corr_id
    # No completion row anywhere sharing this corr_id -- it is unpaired.
    assert not any(
        row.get("kind") == "complete" and row.get("corr_id") == corr_id
        for row in lines
    )


def test_kill_switch_disables_started_row_too(tmp_path, monkeypatch):
    fake_common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )
    monkeypatch.setenv("COORDINATOR_OP_LATENCY_DISABLE", "1")

    record_op_started(
        op="ping", t_start=1.0, corr_id=new_correlation_id(), repo_root=tmp_path,
    )

    sink = _sink_for(fake_common_dir)
    assert not sink.exists()


def test_started_row_unwritable_sink_does_not_raise(tmp_path, monkeypatch):
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory", encoding="utf-8")
    unwritable_common_dir = blocking_file / "impossible-child"

    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir",
        lambda repo_root: unwritable_common_dir,
    )

    # Must not raise.
    record_op_started(
        op="ping", t_start=1.0, corr_id=new_correlation_id(), repo_root=tmp_path,
    )


def test_new_correlation_id_is_unique_and_cheap_shaped():
    a = new_correlation_id()
    b = new_correlation_id()
    assert a != b
    assert "uuid" not in repr(type(a))
    # pid-counter shape: exactly one separating hyphen between two int-parseable halves.
    pid_part, _, counter_part = a.partition("-")
    assert pid_part.isdigit()
    assert counter_part.isdigit()


def test_pairing_summary_counts_paired_and_vanished(tmp_path):
    sink = tmp_path / "op-latency.jsonl"
    paired_id = "1-1"
    vanished_id = "1-2"
    rows = [
        {"kind": "started", "corr_id": paired_id, "t_start": 100.0, "op": "a"},
        {"kind": "complete", "corr_id": paired_id, "t_start": 100.0, "op": "a"},
        {"kind": "started", "corr_id": vanished_id, "t_start": 0.0, "op": "b"},
    ]
    sink.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    summary = pairing_summary(sink_path=sink, staleness_cutoff_secs=40.0, now=1000.0)
    assert summary["total"] == 2
    assert summary["paired"] == 1
    assert summary["unpaired_started"] == 1
    assert summary["in_flight"] == 0
    assert summary["unpaired_rate"] == pytest.approx(0.5)
    assert summary["malformed_lines_skipped"] == 0


def test_pairing_summary_treats_in_flight_started_as_not_vanished(tmp_path):
    sink = tmp_path / "op-latency.jsonl"
    in_flight_id = "1-3"
    rows = [
        {"kind": "started", "corr_id": in_flight_id, "t_start": 990.0, "op": "c"},
    ]
    sink.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    summary = pairing_summary(sink_path=sink, staleness_cutoff_secs=40.0, now=1000.0)
    assert summary["total"] == 1
    assert summary["unpaired_started"] == 0
    assert summary["in_flight"] == 1


def test_pairing_summary_treats_missing_kind_as_complete_never_started(tmp_path):
    sink = tmp_path / "op-latency.jsonl"
    # Pre-C1 row: no "kind" field at all. Per C1's documented backward-reading
    # rule this must be treated as "complete" and MUST NOT be counted as a
    # started row (it has no corr_id either, so it can't pair with anything).
    rows = [
        {"op": "legacy", "t_start": 1.0, "elapsed_ms": 3.0, "outcome": "ok"},
    ]
    sink.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    summary = pairing_summary(sink_path=sink, staleness_cutoff_secs=40.0, now=1000.0)
    assert summary["total"] == 0
    assert summary["paired"] == 0
    assert summary["unpaired_started"] == 0


def test_pairing_summary_tolerates_torn_final_line(tmp_path):
    sink = tmp_path / "op-latency.jsonl"
    good_id = "1-4"
    content = (
        json.dumps({"kind": "started", "corr_id": good_id, "t_start": 0.0, "op": "d"})
        + "\n"
        + '{"kind": "started", "corr_id": "1-5", "t_star'  # torn mid-write
    )
    sink.write_text(content, encoding="utf-8")

    summary = pairing_summary(sink_path=sink, staleness_cutoff_secs=40.0, now=1000.0)
    assert summary["total"] == 1
    assert summary["unpaired_started"] == 1
    assert summary["malformed_lines_skipped"] == 1


def test_pairing_summary_missing_sink_is_empty_not_raising(tmp_path):
    summary = pairing_summary(sink_path=tmp_path / "does-not-exist.jsonl")
    assert summary["total"] == 0
    assert summary["unpaired_rate"] == 0.0


def test_pairing_summary_repo_root_spans_rotated_generations(tmp_path, monkeypatch):
    # C3, plan 2026-08-19-warm-engine-gets-an-honest-instrument: a `started`
    # row in an older rotated generation whose `complete` partner landed in
    # the live sink must still pair when resolved via `repo_root` -- a
    # single-file read of the live sink alone sees the `complete` row with no
    # matching `started` row (harmless) but, more importantly, would show the
    # wrong "total" if the started row were in the newer file instead. This
    # fixture puts `started` in the OLDER generation and `complete` in the
    # LIVE sink, so a single-file (live-sink-only) read counts total=0,
    # paired=0 -- the wrong answer this task exists to fix.
    fake_common_dir = tmp_path / "common"
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )

    logs_dir = fake_common_dir / "coordinator-sessions" / "logs"
    logs_dir.mkdir(parents=True)
    live_sink = logs_dir / "op-latency.jsonl"
    rotated_sink = logs_dir / "op-latency.1.jsonl"

    corr_id = "1-99"
    rotated_sink.write_text(
        json.dumps({"kind": "started", "corr_id": corr_id, "t_start": 0.0, "op": "e"}) + "\n",
        encoding="utf-8",
    )
    live_sink.write_text(
        json.dumps({"kind": "complete", "corr_id": corr_id, "t_start": 0.0, "op": "e"}) + "\n",
        encoding="utf-8",
    )

    summary = pairing_summary(
        repo_root=tmp_path, staleness_cutoff_secs=40.0, now=1000.0
    )
    assert summary["total"] == 1
    assert summary["paired"] == 1
    assert summary["unpaired_started"] == 0

    # The bug this task fixes: reading only the live (single) file loses the
    # `started` row entirely, so pairing reports zero total/paired -- the
    # wrong answer that would previously have been returned by a single-file
    # read routed to `repo_root`.
    single_file_summary = pairing_summary(
        sink_path=live_sink, staleness_cutoff_secs=40.0, now=1000.0
    )
    assert single_file_summary["total"] == 0
    assert single_file_summary["paired"] == 0


# Review: code-reviewer (Finding 1, P2) — the rest of this module exercises
# op_latency.py in isolation, which would pass unmodified even if ipc.py's
# dispatch_message never called record_op_started and never threaded corr_id
# at all. This test drives dispatch_message itself as the bridge check.
async def _telemetry_bridge_handler(params, ctx=None, repo_root=None):
    return {"ok": True}


def test_dispatch_message_writes_paired_started_and_complete_rows(tmp_path, monkeypatch):
    """dispatch_message itself (not just op_latency) writes a started row before
    the handler runs and a complete row after, sharing one corr_id — the actual
    ipc.py wiring, not just the op_latency layer underneath it.

    Verified capable of failing (manual check, not asserted in-suite): with the
    `record_op_started(...)` call site in `dispatch_message` (coordinator_core/ipc.py)
    commented out, this test fails — the sink held only the "complete" row (no
    "started" row for `test.telemetry_bridge`), so `len(started) == 1` failed
    with `len(started) == 0`. The call site was restored afterward and the test
    passes again.
    """
    fake_common_dir = tmp_path / ".git"
    fake_common_dir.mkdir()
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )

    method = "test.telemetry_bridge"
    saved = ipc._REGISTRY.get(method)
    ipc._REGISTRY[method] = _telemetry_bridge_handler
    try:
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {},
            "_origin_worktree": str(tmp_path),
        }
        response = _run(ipc.dispatch_message(msg))
    finally:
        if saved is None:
            ipc._REGISTRY.pop(method, None)
        else:
            ipc._REGISTRY[method] = saved

    assert "result" in response

    sink = _sink_for(fake_common_dir)
    assert sink.exists()
    lines = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines()]

    started = [row for row in lines if row.get("kind") == "started"]
    complete = [row for row in lines if row.get("kind") == "complete"]
    assert len(started) == 1
    assert len(complete) == 1
    assert started[0]["op"] == method
    assert complete[0]["op"] == method
    assert started[0]["corr_id"] is not None
    assert started[0]["corr_id"] == complete[0]["corr_id"]


@pytest.mark.spawns_process
def test_concurrent_appends_do_not_interleave_corrupt_lines(tmp_path):
    sink = tmp_path / "logs" / "op-latency.jsonl"
    sink.parent.mkdir(parents=True, exist_ok=True)

    n_procs = 4
    n_per_proc = 25
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_child_append, args=(str(sink), n_per_proc, idx))
        for idx in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_procs * n_per_proc

    seen_by_idx = {idx: 0 for idx in range(n_procs)}
    for line in lines:
        entry = json.loads(line)  # raises if any line interleave-corrupted
        op = entry["op"]
        assert op.startswith("test.op.")
        idx = int(op.rsplit(".", 1)[1])
        seen_by_idx[idx] += 1

    assert seen_by_idx == {idx: n_per_proc for idx in range(n_procs)}


def _composition_rows(repo_root: Path) -> list:
    sink_dir = repo_root / ".git" / "coordinator-sessions" / "logs"
    rows = []
    for sink in sorted(sink_dir.glob("op-latency*.jsonl")):
        for line in sink.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return [r for r in rows if r.get("kind") == "composition"]


def test_composition_row_carries_a_dateable_t_start(tmp_path):
    """A composition row must be datable to a calendar day on its own.

    Negative-spec: `CompositionBudget.elapsed_secs` runs on `time.monotonic`,
    which carries no date, and sink-file mtime is destroyed by rotation — so
    dropping `t_start` from this row makes the calendar-day partition the
    ceiling derivation depends on (docs/plans/
    2026-08-18-arm-the-composition-budget.md § C4) underivable from the
    record. This test exists so that field cannot be silently dropped again.
    """
    import datetime

    (tmp_path / ".git").mkdir()
    before = time.time()
    record_composition_span(
        composition_id="cid-1",
        name="some_assemble",
        invocation_count=3,
        elapsed_secs=1.5,
        outcome="success",
        t_start=before,
        repo_root=tmp_path,
        sid="sid-1",
    )

    rows = _composition_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row["t_start"], (int, float))
    assert row["t_start"] == pytest.approx(before)
    # the whole point: a reader can name the row's day without any out-of-band state
    assert datetime.datetime.fromtimestamp(row["t_start"]).date() == datetime.date.today()


def test_flush_composition_record_stamps_t_start_at_composition_start(tmp_path):
    """The stamp is the composition's START, not its flush — so a long
    composition is dated to when it began, matching `record_op_latency`'s
    `t_start` semantics rather than drifting by its own duration."""
    from coordinator_core.telemetry.composition_record import (
        flush_composition_record,
        make_fleet_budget,
    )

    (tmp_path / ".git").mkdir()
    started = time.time()
    budget = make_fleet_budget("some_ceremony")
    budget.record_invocation("op.one")
    flush_composition_record(budget, "success", repo_root=tmp_path, sid="sid-2")

    rows = _composition_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["t_start"] == pytest.approx(started, abs=5.0)
    assert rows[0]["name"] == "some_ceremony"


def test_flush_composition_record_resolves_sid_from_the_environment(tmp_path, monkeypatch):
    """A composition row carries the session that produced it.

    Negative-spec: the pinned two-argument call shape
    (`flush_composition_record(budget, outcome)`) threads no sid, so leaving
    the default at `None` wrote `"sid": null` on all 521 composition rows the
    fleet produced between `a360c54eb1c2` and this fix, while ordinary op rows
    over the same window carried 416 distinct real ids. `sid` + `pid` is the
    only key joining a composition to the child ops it ran; without it C4's
    `ceremony.scoped_git_commit` cross-check is undecidable and per-directive
    attribution has nothing but a Windows pid that gets recycled. This test
    exists so the field cannot silently go null again.
    """
    from coordinator_core.telemetry.composition_record import (
        flush_composition_record,
        make_fleet_budget,
    )

    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-from-env")

    budget = make_fleet_budget("some_ceremony")
    budget.record_invocation("op.one")
    flush_composition_record(budget, "success", repo_root=tmp_path)

    rows = _composition_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["sid"] == "sess-from-env"


def test_flush_composition_record_sid_precedence_matches_the_fleet_chain(tmp_path, monkeypatch):
    """`COORDINATOR_SESSION_ID` wins over `CLAUDE_SESSION_ID`, which wins over
    `CLAUDE_CODE_SESSION_ID` — the same chain `baton_assemble/__init__.py` and
    `ops/handoff_correct_body.py` resolve on. Three copies of this precedence
    exist by documented precedent; this pins the one that dates the sink so it
    cannot drift from the other two unnoticed."""
    from coordinator_core.telemetry.composition_record import (
        flush_composition_record,
        make_fleet_budget,
    )

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-lowest")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-middle")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-highest")

    budget = make_fleet_budget("some_ceremony")
    flush_composition_record(budget, "success", repo_root=tmp_path)
    assert _composition_rows(tmp_path)[0]["sid"] == "sess-highest"


def test_pinned_flush_shape_leaves_no_field_null(tmp_path, monkeypatch):
    """THE CLASS PIN: the pinned two-argument flush writes a row with no null
    field -- including any field added after this test was written.

    Negative-spec, and the reason this is a loop over `row.items()` rather
    than a list of named assertions: `record_composition_span` has now shipped
    TWICE with a field the flush never threaded, so the field landed `null` on
    every production row while the writer looked correct in isolation.
    `t_start` (fixed 2026-08-18) made C4's calendar-day partition underivable;
    `sid` (fixed 2026-08-19, `c2cb17186`) made its `ceremony.scoped_git_commit`
    cross-check undecidable across 521 rows. Both were found by reading the
    sink, months of fleet traffic after the fact -- never by a test.

    A per-field assertion would not have caught either one, because neither
    field existed when the earlier tests were written. This test fails on the
    NEXT such field without being updated: add a key to the row that the
    pinned call shape cannot populate, and this goes red immediately.

    `flush_composition_record(budget, outcome)` is called with exactly the two
    positional arguments the 8 `apply.py` call sites pass (§ PINNED-INTERFACE);
    `repo_root` is injected only because the test runner's cwd is not the
    fixture repo. A field that cannot be resolved from that shape is a field
    the fleet will write as null.
    """
    from coordinator_core.telemetry.composition_record import (
        flush_composition_record,
        make_fleet_budget,
    )

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-production-like")

    budget = make_fleet_budget("some_ceremony")
    budget.record_invocation("op.one")
    flush_composition_record(budget, "success", repo_root=tmp_path)

    rows = _composition_rows(tmp_path)
    assert len(rows) == 1
    nulls = sorted(k for k, v in rows[0].items() if v is None)
    assert not nulls, (
        f"composition row fields are null under the pinned call shape: {nulls}. "
        "Every field must be resolvable from flush_composition_record(budget, outcome) "
        "alone -- the 8 apply.py call sites thread nothing else. Give the field a "
        "default inside _flush_or_raise (see repo_root and sid) rather than adding a "
        "parameter no production caller passes."
    )


def test_flush_composition_record_explicit_sid_still_wins(tmp_path, monkeypatch):
    """An explicitly passed sid is never overridden by the environment — the
    env chain is a DEFAULT for the pinned call shape, not a substitution."""
    from coordinator_core.telemetry.composition_record import (
        flush_composition_record,
        make_fleet_budget,
    )

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-from-env")

    budget = make_fleet_budget("some_ceremony")
    flush_composition_record(budget, "success", repo_root=tmp_path, sid="sess-explicit")
    assert _composition_rows(tmp_path)[0]["sid"] == "sess-explicit"


# ---------------------------------------------------------------------------
# error_kind -- the failure's identity, not merely that it failed
# ---------------------------------------------------------------------------


def _only_row(tmp_path, monkeypatch, **kwargs):
    """Record one latency row against an isolated sink and return it parsed."""
    fake_common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )
    record_op_latency(op="probe.op", t_start=1.0, elapsed_ms=2.0, repo_root=tmp_path, **kwargs)
    lines = _sink_for(fake_common_dir).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


class TestErrorKind:
    """`error_code` alone is unreadable: `-32603 Internal error` is the modal
    value and it names nothing. Measured 2026-08-27 over the op-latency sink,
    `review_trail.write` had failed 1974 of 3764 calls and `queue.append` 150
    of 918 -- neither population could be told apart on disk, and both had to
    be reproduced by hand against a live engine to learn what they were.
    """

    def test_error_kind_is_recorded(self, tmp_path, monkeypatch):
        row = _only_row(
            tmp_path, monkeypatch, outcome="error",
            error_kind="ValueError: reviewer 'x' is invalid",
        )
        assert row["error_kind"] == "ValueError: reviewer 'x' is invalid"

    def test_error_kind_is_bounded(self, tmp_path, monkeypatch):
        row = _only_row(
            tmp_path, monkeypatch, outcome="error",
            error_kind="ValueError: " + ("x" * 500),
        )
        assert row["error_kind"].startswith("ValueError: ")
        assert len(row["error_kind"]) <= _ERROR_KIND_MAX_CHARS, (
            "an unbounded message can carry a path, a range, or a caller's "
            "parameter values into a sink every peer on the box reads"
        )
        assert row["error_kind"].endswith("…")

    def test_newlines_are_flattened(self, tmp_path, monkeypatch):
        """The sink is JSONL; a multi-line message turns a readable column into
        a wall."""
        row = _only_row(
            tmp_path, monkeypatch, outcome="error",
            error_kind="ValueError: first\n  second\n\tthird",
        )
        assert row["error_kind"] == "ValueError: first second third"

    def test_absent_error_kind_is_null_not_missing(self, tmp_path, monkeypatch):
        """An ok row still carries the key, so a reader never has to tell "this
        op did not fail" from "this row predates the field" -- the exact
        ambiguity that left 1969 of 1976 failures unreadable."""
        row = _only_row(tmp_path, monkeypatch, outcome="ok")
        assert "error_kind" in row
        assert row["error_kind"] is None


# ---------------------------------------------------------------------------
# record_fact_span / kind: "fact_span" (C1, plan
# 2026-08-27-the-fact-layer-is-measured-on-the-one-hot-path) — a NEW row
# kind, routed through neither record_op_latency nor record_composition_span
# (see record_fact_span's own docstring). This section is the "confirmed in
# two of three readers" gap's mechanical closure: pairing_summary,
# cost_census.run_census, and breach_summary must all ignore this kind.
# ---------------------------------------------------------------------------


def _fact_span_rows(repo_root: Path) -> list:
    sink_dir = repo_root / ".git" / "coordinator-sessions" / "logs"
    rows = []
    for sink in sorted(sink_dir.glob("op-latency*.jsonl")):
        for line in sink.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return [r for r in rows if r.get("kind") == "fact_span"]


def test_record_fact_span_shape(tmp_path):
    (tmp_path / ".git").mkdir()
    before = time.time()
    record_fact_span(
        fact="session_facts.session_magnitude_attributed",
        t_start=before,
        elapsed_ms=1.25,
        outcome="computed",
        repo_root=tmp_path,
        sid="sid-fact-1",
    )

    rows = _fact_span_rows(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "fact_span"
    assert row["fact"] == "session_facts.session_magnitude_attributed"
    assert row["outcome"] == "computed"
    assert row["sid"] == "sid-fact-1"
    assert row["t_start"] == pytest.approx(before)
    assert row["elapsed_ms"] == pytest.approx(1.25)
    assert "op" not in row


def test_pairing_summary_ignores_fact_span_rows(tmp_path):
    """A fact_span row carries no `corr_id`, so it cannot be mistaken for
    either a `started` or a `complete` op row."""
    (tmp_path / ".git").mkdir()
    record_fact_span(
        fact="session_facts.session_terminal_sizings",
        t_start=time.time(),
        elapsed_ms=2.0,
        outcome="degraded",
        repo_root=tmp_path,
    )

    summary = pairing_summary(repo_root=tmp_path)
    assert summary["total"] == 0
    assert summary["paired"] == 0
    assert summary["unpaired_started"] == 0


def test_cost_census_ignores_fact_span_rows(tmp_path):
    from coordinator_core.telemetry import cost_census

    (tmp_path / ".git").mkdir()
    record_fact_span(
        fact="session_facts.session_fold_sidecars",
        t_start=time.time(),
        elapsed_ms=3.0,
        outcome="computed",
        repo_root=tmp_path,
    )

    row = cost_census.run_census(repo_root=tmp_path, write=False)
    assert row["rows_matched"] == 0


def test_breach_summary_ignores_fact_span_rows():
    """`breach_summary` is pure over already-parsed rows — a `fact_span` row
    has no `op` key, so it is dropped by the `isinstance(op_name, str)` guard
    without ever contributing to any op's breach count."""
    entries = [
        {
            "kind": "fact_span",
            "fact": "session_facts.session_diff_brightline",
            "t_start": 100.0,
            "elapsed_ms": 99999.0,
            "outcome": "computed",
        },
    ]
    result = breach_summary(entries)
    assert result["totals"]["complete_rows"] == 0
    assert result["totals"]["over_bar"] == 0
    assert result["ops"] == []


class TestTailEntriesKeepsNewestRows:
    """`tail_entries` promises "newest rows kept" and argues in its own
    docstring that recency is what both its consumers need. A `max_rows` cap
    applied from the head of the byte window silently returns the OLDEST rows
    and makes every recently-appended row invisible — which is how a live
    corpus reads as empty to a bounded reader."""

    def _sink(self, tmp_path: Path, rows: int) -> Path:
        path = tmp_path / "op-latency.jsonl"
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(rows):
                fh.write(json.dumps({"t_start": float(i), "seq": i}) + "\n")
        return path

    def test_max_rows_keeps_the_newest_rows_not_the_oldest(self, tmp_path):
        path = self._sink(tmp_path, 500)

        entries, _ = tail_entries(path, tail_bytes=8 * 1024 * 1024, max_rows=10)

        assert len(entries) == 10
        assert [e["seq"] for e in entries] == list(range(490, 500))

    def test_a_row_appended_last_survives_the_cap(self, tmp_path):
        path = self._sink(tmp_path, 300)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t_start": 999.0, "seq": 300, "kind": "fact_span"}) + "\n")

        entries, _ = tail_entries(path, tail_bytes=8 * 1024 * 1024, max_rows=50)

        assert any(e.get("kind") == "fact_span" for e in entries)
        assert entries[-1]["seq"] == 300

    def test_under_the_cap_every_row_is_returned_in_file_order(self, tmp_path):
        path = self._sink(tmp_path, 25)

        entries, head_truncated = tail_entries(path, tail_bytes=8 * 1024 * 1024, max_rows=1000)

        assert head_truncated is False
        assert [e["seq"] for e in entries] == list(range(25))
