"""CloserRegistry: LIFO, retained-on-failure, aggregate error."""

from __future__ import annotations

import pytest

from coordinator_core.resource_lifecycle import CloserRegistry


def test_empty_close_is_noop():
    CloserRegistry().close()


def test_runs_lifo_and_drops_successes():
    order: list[str] = []
    reg = CloserRegistry()
    reg.register("a", lambda: order.append("a"))
    reg.register("b", lambda: order.append("b"))
    reg.close()
    assert order == ["b", "a"]
    assert reg.pending() == []
    reg.close()
    assert order == ["b", "a"]


def test_failed_closer_stays_owned_and_is_retried():
    calls = {"n": 0}
    ran: list[str] = []

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("busy")

    reg = CloserRegistry()
    reg.register("flaky", flaky)
    reg.register("ok", lambda: ran.append("ok"))
    with pytest.raises(ExceptionGroup) as ei:
        reg.close()
    assert [type(e) for e in ei.value.exceptions] == [OSError]
    assert ran == ["ok"]
    assert reg.pending() == ["flaky"]
    reg.close()
    assert calls["n"] == 2
    assert reg.pending() == []


def test_all_failures_aggregated_and_later_closers_still_run():
    reg = CloserRegistry()

    def boom_a():
        raise ValueError("a")

    def boom_b():
        raise KeyError("b")

    reg.register("a", boom_a)
    reg.register("b", boom_b)
    with pytest.raises(ExceptionGroup) as ei:
        reg.close()
    assert [type(e) for e in ei.value.exceptions] == [KeyError, ValueError]
    assert reg.pending() == ["a", "b"]


def test_closer_may_register_during_close():
    reg = CloserRegistry()
    reg.register("a", lambda: reg.register("late", lambda: None))
    reg.close()
    assert reg.pending() == ["late"]
