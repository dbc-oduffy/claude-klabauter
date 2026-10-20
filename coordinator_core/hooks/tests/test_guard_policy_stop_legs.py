"""Stop-leg hook gates resolve their deny at the guard-level policy point."""

from __future__ import annotations

import os

import pytest

from coordinator_core import machine_profile as mp
from coordinator_core.hooks import (
    guard_manufactured_blocker,
    guard_terminal_review,
    watchdog_undischarged_next_move,
)
from coordinator_core.hooks._envelope import deny

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture()
def reg(tmp_path, monkeypatch):
    d = tmp_path / "reg"
    d.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(d))
    for k in list(os.environ):
        if k.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(k)
    mp.reset_cache()
    yield d
    mp.reset_cache()


def _write(reg, text):
    (reg / "registry.local.toml").write_text(text, encoding="utf-8")
    mp.reset_cache()


def _denies(result) -> bool:
    return result.get("decision") == "block" and "BLOCK-TEXT" in str(result.get("reason", ""))


def _gated(name):
    return mp.apply_guard_level(name, deny("Stop", "BLOCK-TEXT"))


def test_default_is_advisory_with_the_deny_text_as_context(reg):
    result = _gated(guard_manufactured_blocker.GUARD_NAME)
    assert not _denies(result)
    assert "BLOCK-TEXT" in result["hookSpecificOutput"]["additionalContext"]


def test_global_strict_denies(reg):
    _write(reg, '"coordinator.guard_level" = "strict"\n')
    assert _denies(_gated(guard_manufactured_blocker.GUARD_NAME))


def test_per_guard_strict_hardens_only_that_guard(reg):
    _write(
        reg,
        '"coordinator.guard_level" = "warn"\n'
        '"coordinator.guard_level.guard-manufactured-blocker" = "strict"\n',
    )
    assert _denies(_gated(guard_manufactured_blocker.GUARD_NAME))
    assert not _denies(_gated(guard_terminal_review.GUARD_NAME))


@pytest.mark.parametrize(
    "mod, name",
    [
        (guard_manufactured_blocker, "guard-manufactured-blocker"),
        (guard_terminal_review, "guard-terminal-review"),
        (watchdog_undischarged_next_move, "watchdog-undischarged-next-move"),
    ],
)
def test_guard_name_constant(mod, name):
    assert mod.GUARD_NAME == name
