"""Tests for coordinator_core.warm.lifecycle.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C17

Two guard classes, mirroring coordinator/tests/test_claude_doe_launch_waits.py's
split:

  - Source-shape (AST): pins that the shared shutdown tail (`_run_tail`)
    calls `exit_fn` (the injected `os._exit`) as its last statement and
    never calls `sys.exit` anywhere in the module -- platform-agnostic,
    catches a regression without ever calling the real `os._exit`.
  - Behavioural: exercises `begin_shutdown` / `drain_and_exit` for real,
    with `exit_fn` replaced by a recording fake so the test process is
    never actually terminated -- the seam this module's own docstring
    names (`exit_fn`), not a subprocess.
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import lifecycle

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "coordinator_core" / "warm" / "lifecycle.py"


@pytest.fixture(autouse=True)
def _clean_guard():
    lifecycle.reset_shutdown_guard_for_test()
    yield
    lifecycle.reset_shutdown_guard_for_test()


# ---------------------------------------------------------------------------
# Source-shape guard
# ---------------------------------------------------------------------------


def _run_tail_func() -> ast.FunctionDef:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_run_tail":
            return node
    raise AssertionError("lifecycle.py: _run_tail() not found")


def test_run_tail_calls_exit_fn_as_its_last_statement():
    tail = _run_tail_func()
    last = tail.body[-1]
    assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call), (
        "_run_tail's last statement must be a call expression (exit_fn(0))"
    )
    assert ast.unparse(last.value.func) == "exit_fn", (
        f"_run_tail's last statement must call exit_fn, found {ast.unparse(last.value.func)!r} -- "
        "step 4 (os._exit) must be the final action of the shutdown tail"
    )


def test_run_tail_never_calls_sys_exit():
    tail = _run_tail_func()
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(tail)
        if isinstance(node, ast.Call)
    ]
    assert "sys.exit" not in calls and "exit" not in calls, (
        f"_run_tail must never call sys.exit (os._exit skips atexit; sys.exit does not) -- "
        f"found calls: {calls}"
    )


def test_module_never_calls_sys_exit_anywhere():
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    assert "sys.exit" not in calls, (
        f"lifecycle.py must never call sys.exit anywhere -- found calls: {calls}"
    )


def test_ctx_shutdown_precedes_exit_fn_in_run_tail():
    tail = _run_tail_func()
    call_names = []
    for node in ast.walk(tail):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            call_names.append(node.func.id)
    assert "ctx_shutdown" in call_names and "exit_fn" in call_names, (
        f"_run_tail must call both ctx_shutdown and exit_fn, found: {call_names}"
    )
    assert call_names.index("ctx_shutdown") < call_names.index("exit_fn"), (
        "ctx_shutdown() must precede exit_fn() -- os._exit skips atexit, so any "
        "cleanup not already run by then never runs"
    )


# ---------------------------------------------------------------------------
# Behavioural guard -- exit_fn is a recording fake, never the real os._exit
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)


def test_begin_shutdown_runs_full_sequence_in_order():
    order = []
    result = lifecycle.begin_shutdown(
        close_listener=lambda: order.append("close_listener"),
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: order.append("ctx_shutdown"),
        exit_fn=lambda code: order.append(("exit_fn", code)),
    )
    assert result is True
    assert order == ["close_listener", "ctx_shutdown", ("exit_fn", 0)]


def test_drain_and_exit_skips_close_listener():
    order = []
    result = lifecycle.drain_and_exit(
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: order.append("ctx_shutdown"),
        exit_fn=lambda code: order.append(("exit_fn", code)),
    )
    assert result is True
    assert order == ["ctx_shutdown", ("exit_fn", 0)]


def test_waits_for_in_flight_to_reach_zero_before_ctx_shutdown():
    counts = [2, 1, 1, 0]
    order = []

    def in_flight_count():
        return counts.pop(0) if counts else 0

    lifecycle.begin_shutdown(
        close_listener=lambda: None,
        in_flight_count=in_flight_count,
        ctx_shutdown=lambda: order.append("ctx_shutdown"),
        exit_fn=lambda code: order.append("exit_fn"),
        drain_ceiling_secs=5.0,
    )
    assert not counts, "ctx_shutdown ran before in_flight_count reached zero"
    assert order == ["ctx_shutdown", "exit_fn"]


def test_drain_ceiling_is_bounded_not_unlimited():
    """A permanently-nonzero in_flight_count must not hang the sequence --
    the ceiling bounds shutdown latency instead."""
    started = time.monotonic()
    order = []
    lifecycle.begin_shutdown(
        close_listener=lambda: None,
        in_flight_count=lambda: 1,  # never reaches zero
        ctx_shutdown=lambda: order.append("ctx_shutdown"),
        exit_fn=lambda code: order.append("exit_fn"),
        drain_ceiling_secs=0.2,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"shutdown sequence did not respect its ceiling: {elapsed:.2f}s"
    assert order == ["ctx_shutdown", "exit_fn"], (
        "hitting the ceiling must still fall through to ctx_shutdown then exit_fn"
    )


def test_single_shot_second_caller_is_a_no_op():
    first = lifecycle.begin_shutdown(
        close_listener=lambda: None,
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: None,
        exit_fn=lambda code: None,
    )
    second_order = []
    second = lifecycle.begin_shutdown(
        close_listener=lambda: second_order.append("close_listener"),
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: second_order.append("ctx_shutdown"),
        exit_fn=lambda code: second_order.append("exit_fn"),
    )
    assert first is True
    assert second is False
    assert second_order == [], "a losing caller must not touch any of the four steps"


def test_single_shot_under_concurrent_triggers():
    """staff-eng finding 13: an idle demotion (begin_shutdown) and a skew
    eviction (drain_and_exit) firing near-simultaneously must both attempt
    entry, but only one may ever reach ctx_shutdown / exit_fn."""
    winners = []
    winners_lock = threading.Lock()
    barrier = threading.Barrier(2)

    exit_recorder = _Recorder()
    ctx_recorder = _Recorder()

    def run_begin_shutdown():
        barrier.wait()
        won = lifecycle.begin_shutdown(
            close_listener=lambda: None,
            in_flight_count=lambda: 0,
            ctx_shutdown=ctx_recorder,
            exit_fn=exit_recorder,
        )
        if won:
            with winners_lock:
                winners.append("idle")

    def run_drain_and_exit():
        barrier.wait()
        won = lifecycle.drain_and_exit(
            in_flight_count=lambda: 0,
            ctx_shutdown=ctx_recorder,
            exit_fn=exit_recorder,
        )
        if won:
            with winners_lock:
                winners.append("skew")

    t1 = threading.Thread(target=run_begin_shutdown)
    t2 = threading.Thread(target=run_drain_and_exit)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(ctx_recorder.calls) == 1, (
        f"ctx_shutdown must run exactly once across concurrent triggers, "
        f"ran {len(ctx_recorder.calls)} times"
    )
    assert len(exit_recorder.calls) == 1, (
        f"exit_fn must run exactly once across concurrent triggers, "
        f"ran {len(exit_recorder.calls)} times"
    )


def test_drain_ceiling_secs_default_tracks_dispatch_timeout(monkeypatch):
    import coordinator_core.ipc as ipc

    monkeypatch.setattr(ipc, "DISPATCH_TIMEOUT_SECS", 10.0)
    monkeypatch.delenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", raising=False)
    assert lifecycle._drain_ceiling_secs() == pytest.approx(
        10.0 + lifecycle.DRAIN_CEILING_MARGIN_SECS
    )
