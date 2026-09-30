"""The pytest-only child carve-out of the stamp gate needs both pytest markers."""
from __future__ import annotations

import pytest

from coordinator_core import ipc

_ENV = ipc.PYTEST_UNSTAMPED_DISPATCH_ENV


@pytest.fixture
def gate_closed(monkeypatch):
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", False)


def test_honoured_when_both_pytest_markers_present(gate_closed, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x (call)")
    monkeypatch.setenv(_ENV, "1")
    assert ipc.allow_unstamped_dispatch_under_pytest() is True
    assert ipc.is_unstamped_dispatch_allowed() is True


def test_refused_without_the_suite_opt_in(gate_closed, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x (call)")
    monkeypatch.delenv(_ENV, raising=False)
    assert ipc.allow_unstamped_dispatch_under_pytest() is False
    assert ipc.is_unstamped_dispatch_allowed() is False


def test_refused_outside_a_running_test(gate_closed, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv(_ENV, "1")
    assert ipc.allow_unstamped_dispatch_under_pytest() is False
    assert ipc.is_unstamped_dispatch_allowed() is False
