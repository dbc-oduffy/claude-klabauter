"""A 19-path ceremony.commit_v2 replies under the 500ms process budget or its phase row names the blocked leg."""
from __future__ import annotations


import pytest

from coordinator_core.ops.ceremony import commit_v2_phases as cp

_SID = "11111111-2222-4333-8444-555555555555"


def _clocks(monkeypatch, cpu, wall):
    c, w = iter(cpu), iter(wall)
    monkeypatch.setattr(cp.time, "thread_time", lambda: next(c))
    monkeypatch.setattr(cp.time, "monotonic", lambda: next(w))


def test_wait_leg_past_budget_is_named(monkeypatch):
    _clocks(monkeypatch, [0, 0.01, 0.01, 0.02], [0, 0.01, 0.01, 2.0])
    led = cp.PhaseLedger()
    with led.phase("cheap"):
        pass
    with led.phase("claim_poll"):
        pass
    blocked = led.blocked_leg()
    assert blocked["leg"] == "claim_poll"
    assert blocked["kind"] == "wait"
    assert blocked["wait_ms"] > 1900


def test_compute_over_budget_names_costliest_phase(monkeypatch):
    _clocks(monkeypatch, [0, 0.1, 0.1, 0.9], [0, 0.1, 0.1, 0.9])
    led = cp.PhaseLedger()
    with led.phase("small"):
        pass
    with led.phase("big"):
        pass
    assert led.blocked_leg()["leg"] == "big"
    assert led.blocked_leg()["kind"] == "compute"


def test_in_budget_names_no_leg(monkeypatch):
    _clocks(monkeypatch, [0, 0.01], [0, 0.05])
    led = cp.PhaseLedger()
    with led.phase("x"):
        pass
    assert led.blocked_leg() is None
