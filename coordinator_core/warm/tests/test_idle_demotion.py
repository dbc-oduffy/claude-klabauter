"""Tests for coordinator_core.warm.idle.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C24
"""

from __future__ import annotations

import inspect

import pytest

from coordinator_core.warm import idle, lifecycle, skew


@pytest.fixture(autouse=True)
def _reset_state():
    idle.reset_idle_clock_for_test()
    lifecycle.reset_shutdown_guard_for_test()
    yield
    idle.reset_idle_clock_for_test()
    lifecycle.reset_shutdown_guard_for_test()


def _served(n: int) -> idle.ServedCountFn:
    return lambda: n


# ---------------------------------------------------------------------------
# seconds_idle / mark_invocation
# ---------------------------------------------------------------------------


def test_seconds_idle_counts_from_last_mark_invocation():
    fake_now = [0.0]
    clock = lambda: fake_now[0]

    idle.mark_invocation(clock=clock)
    fake_now[0] = 12.5

    assert idle.seconds_idle(clock=clock) == pytest.approx(12.5)


def test_seconds_idle_counts_from_process_start_when_never_invoked(monkeypatch):
    monkeypatch.setattr(idle, "_process_start_monotonic", 100.0)
    fake_now = [130.0]

    assert idle.seconds_idle(clock=lambda: fake_now[0]) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# resolve_idle_deadline_secs -- extends C23's engine.warm.* namespace
# ---------------------------------------------------------------------------


def test_resolve_idle_deadline_defaults_to_15_minutes(monkeypatch):
    monkeypatch.setattr(idle, "registry_get", lambda key: None)
    assert idle.resolve_idle_deadline_secs() == pytest.approx(15 * 60.0)


def test_resolve_idle_deadline_reads_registry_key(monkeypatch):
    seen = []

    def fake_registry_get(key):
        seen.append(key)
        return "5"

    monkeypatch.setattr(idle, "registry_get", fake_registry_get)
    assert idle.resolve_idle_deadline_secs() == pytest.approx(5 * 60.0)
    assert seen == [idle.REGISTRY_KEY_IDLE_MINUTES]


def test_resolve_idle_deadline_falls_back_on_unparseable_value(monkeypatch):
    monkeypatch.setattr(idle, "registry_get", lambda key: "not-a-number")
    assert idle.resolve_idle_deadline_secs() == pytest.approx(15 * 60.0)


def test_resolve_idle_deadline_falls_back_on_non_positive_value(monkeypatch):
    monkeypatch.setattr(idle, "registry_get", lambda key: "0")
    assert idle.resolve_idle_deadline_secs() == pytest.approx(15 * 60.0)


# ---------------------------------------------------------------------------
# should_demote -- full deadline and zero-served short-circuit
# ---------------------------------------------------------------------------


def test_should_demote_false_before_deadline():
    fake_now = [0.0]
    clock = lambda: fake_now[0]
    idle.mark_invocation(clock=clock)
    fake_now[0] = 60.0

    assert (
        idle.should_demote(
            served_count=_served(3), idle_deadline_secs=15 * 60.0, clock=clock
        )
        is False
    )


def test_should_demote_true_after_full_idle_deadline():
    fake_now = [0.0]
    clock = lambda: fake_now[0]
    idle.mark_invocation(clock=clock)
    fake_now[0] = 15 * 60.0 + 1.0

    assert (
        idle.should_demote(
            served_count=_served(3), idle_deadline_secs=15 * 60.0, clock=clock
        )
        is True
    )


def test_zero_served_deadline_demotes_immediately_before_full_deadline():
    """Staff-eng finding 5: served_count() == 0 past the short deadline
    demotes even though the 15-minute idle deadline has not elapsed."""
    fake_now = [0.0]
    clock = lambda: fake_now[0]
    idle.mark_invocation(clock=clock)
    fake_now[0] = idle.ZERO_SERVED_DEADLINE_SECS + 1.0

    assert (
        idle.should_demote(
            served_count=_served(0), idle_deadline_secs=15 * 60.0, clock=clock
        )
        is True
    )


def test_zero_served_deadline_does_not_fire_when_something_was_served():
    fake_now = [0.0]
    clock = lambda: fake_now[0]
    idle.mark_invocation(clock=clock)
    fake_now[0] = idle.ZERO_SERVED_DEADLINE_SECS + 1.0

    assert (
        idle.should_demote(
            served_count=_served(1), idle_deadline_secs=15 * 60.0, clock=clock
        )
        is False
    )


def test_zero_served_deadline_does_not_fire_before_its_own_deadline():
    fake_now = [0.0]
    clock = lambda: fake_now[0]
    idle.mark_invocation(clock=clock)
    fake_now[0] = idle.ZERO_SERVED_DEADLINE_SECS - 1.0

    assert (
        idle.should_demote(
            served_count=_served(0), idle_deadline_secs=15 * 60.0, clock=clock
        )
        is False
    )


# ---------------------------------------------------------------------------
# demote_if_idle -- calls C17's begin_shutdown verbatim, never reimplements
# ---------------------------------------------------------------------------


def test_demote_if_idle_calls_begin_shutdown_when_due():
    fake_now = [0.0]
    clock = lambda: fake_now[0]
    idle.mark_invocation(clock=clock)
    fake_now[0] = 15 * 60.0 + 1.0

    order = []
    exited = []

    result = idle.demote_if_idle(
        served_count=_served(3),
        close_listener=lambda: order.append("close"),
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: order.append("ctx_shutdown"),
        exit_fn=lambda code: exited.append(code),
        idle_deadline_secs=15 * 60.0,
        clock=clock,
    )

    assert result is True
    assert order == ["close", "ctx_shutdown"]
    assert exited == [0]


def test_demote_if_idle_noop_when_not_due():
    fake_now = [0.0]
    clock = lambda: fake_now[0]
    idle.mark_invocation(clock=clock)
    fake_now[0] = 1.0

    order = []
    result = idle.demote_if_idle(
        served_count=_served(3),
        close_listener=lambda: order.append("close"),
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: order.append("ctx_shutdown"),
        exit_fn=lambda code: order.append(("exit", code)),
        idle_deadline_secs=15 * 60.0,
        clock=clock,
    )

    assert result is False
    assert order == []


def test_demote_if_idle_shares_single_shot_guard_with_a_concurrent_skew_drain():
    """Reclaim, not adjudicate: proves the two paths share ONLY C17's
    single-shot guard. Simulate a skew eviction winning the guard first
    (via drain_and_exit), then an idle demotion firing concurrently --
    the idle path must observe it lost and must not run its own tail."""
    fake_now = [0.0]
    clock = lambda: fake_now[0]
    idle.mark_invocation(clock=clock)
    fake_now[0] = 15 * 60.0 + 1.0

    skew_order = []
    skew_won = lifecycle.drain_and_exit(
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: skew_order.append("ctx_shutdown"),
        exit_fn=lambda code: skew_order.append(("exit", code)),
    )
    assert skew_won is True

    idle_order = []
    idle_result = idle.demote_if_idle(
        served_count=_served(3),
        close_listener=lambda: idle_order.append("close"),
        in_flight_count=lambda: 0,
        ctx_shutdown=lambda: idle_order.append("ctx_shutdown"),
        exit_fn=lambda code: idle_order.append(("exit", code)),
        idle_deadline_secs=15 * 60.0,
        clock=clock,
    )

    assert idle_result is False
    assert idle_order == []


# ---------------------------------------------------------------------------
# THE INVARIANT: idle never gates/delays/vetoes skew, skew never reads idle
# ---------------------------------------------------------------------------


def test_evict_on_skew_signature_carries_no_idle_input():
    params = set(inspect.signature(skew.evict_on_skew).parameters)
    forbidden = {"idle", "idleness", "idle_deadline_secs", "served_count", "should_demote"}
    assert not (params & forbidden)


def test_skew_module_does_not_import_idle_module():
    assert not hasattr(skew, "idle")


def test_idle_module_calls_begin_shutdown_not_a_second_sequence():
    """Pins that this module reuses C17's sequence verbatim rather than
    authoring a second one: `demote_if_idle`'s body calls
    `warm.lifecycle.begin_shutdown` and never calls `exit_fn` itself --
    the only `os._exit` reference in the source is the parameter default,
    not a second exit call inside the function body."""
    source = inspect.getsource(idle.demote_if_idle)
    body = source.split(") -> bool:", 1)[1]
    assert "begin_shutdown(" in body
    assert "os._exit" not in body
    assert "exit_fn(" not in body
