"""Ratchet: each registered lint's existing-hit count equals its pin, so the count only falls.

Does not run any lint engine; it counts the ledger, whose agreement with live hits is
enforced by the lint's own stale-entry tests.
"""
from __future__ import annotations

import dataclasses

import pytest

from coordinator_core.tests import _lint_backfill_registry as registry


@pytest.mark.parametrize("name", sorted(registry.REGISTRY))
def test_live_count_equals_pin(name):
    assert registry.check(name) == []


@pytest.mark.parametrize("name", sorted(registry.REGISTRY))
def test_pin_is_a_nonnegative_int(name):
    pin = registry.REGISTRY[name].pin
    assert type(pin) is int and pin >= 0


def test_home_resolution_lint_is_registered():
    assert "home-resolution-lint" in registry.REGISTRY


def _with_live(monkeypatch, name, delta):
    entry = registry.REGISTRY[name]
    live = entry.count() + delta
    monkeypatch.setitem(
        registry.REGISTRY, name, dataclasses.replace(entry, count=lambda: live)
    )


def test_one_more_hit_fails_as_regrew(monkeypatch):
    _with_live(monkeypatch, "home-resolution-lint", +1)
    problems = registry.check("home-resolution-lint")
    assert len(problems) == 1 and "regrew" in problems[0]


def test_one_fewer_hit_fails_as_stale_pin(monkeypatch):
    _with_live(monkeypatch, "home-resolution-lint", -1)
    problems = registry.check("home-resolution-lint")
    assert len(problems) == 1 and "lower the pin" in problems[0]
