"""Pin for `wire_contract.publish_contention_wait_secs` (C1,
docs/plans/2026-08-30-a-second-percolate-round-stops-sleeping.md).

Default posture is deny-at-once (0.0); `COORDINATOR_ALLOW_PERCOLATE_QUEUE`
opts back into the existing `contended_lock_wait_secs()` clamp/ceiling, with
no second source of truth for the 180s number.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import wire_contract  # noqa: E402
from coordinator_core.locked_write import (  # noqa: E402
    CONTENDED_LOCK_WAIT_ENV,
    contended_lock_wait_secs,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, raising=False)
    monkeypatch.delenv(CONTENDED_LOCK_WAIT_ENV, raising=False)


def test_default_denies_at_once():
    assert wire_contract.publish_contention_wait_secs() == 0.0


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "FALSE", "Off"])
def test_every_falsy_spelling_denies_at_once(monkeypatch, falsy):
    monkeypatch.setenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, falsy)
    assert wire_contract.publish_contention_wait_secs() == 0.0


def test_key_set_resolves_contended_lock_wait_secs(monkeypatch):
    monkeypatch.setenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, "1")
    assert wire_contract.publish_contention_wait_secs() == contended_lock_wait_secs()


def test_key_set_alongside_narrowing_override_resolves_narrowed(monkeypatch):
    monkeypatch.setenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, "1")
    monkeypatch.setenv(CONTENDED_LOCK_WAIT_ENV, "30")
    assert wire_contract.publish_contention_wait_secs() == 30.0


def test_key_set_alongside_over_ceiling_override_clamps_at_ceiling(monkeypatch):
    monkeypatch.setenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, "1")
    monkeypatch.setenv(CONTENDED_LOCK_WAIT_ENV, "900")
    assert wire_contract.publish_contention_wait_secs() == 180.0
