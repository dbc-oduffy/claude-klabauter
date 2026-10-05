"""Stop-leg hook gates resolve their deny at the guard-level policy point."""

from __future__ import annotations

import asyncio
import os

import pytest

from coordinator_core import machine_profile as mp
from coordinator_core.hooks import (
    guard_kira_verdict_routed,
    guard_manufactured_blocker,
    guard_terminal_review,
    watchdog_undischarged_next_move,
)
from coordinator_core.hooks._envelope import deny
from coordinator_core.hooks.stop_dispatch import _handler

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


def _kira_payload(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    share = repo / ".coordinator-local" / "subagent-share" / "sess-1"
    share.mkdir(parents=True)
    (share / "coordinatoroverengineering-reviewer.abc.md").write_text(
        "---\nagent_type: coordinator:overengineering-reviewer\n"
        "spawned_at: 2026-08-31T00:00:00Z\nfindings_count: 2\n---\n",
        encoding="utf-8",
    )
    return {"cwd": str(repo), "session_id": "sess-1"}


def _denies(result) -> bool:
    hso = result.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision") == "deny"


def _run(payload):
    return asyncio.run(_handler({"payload": payload}))


def test_default_is_advisory_with_kira_text_as_context(reg, tmp_path):
    result = _run(_kira_payload(tmp_path))
    assert not _denies(result)
    assert "unrouted Kira" in result["hookSpecificOutput"]["additionalContext"]


def test_global_strict_denies(reg, tmp_path):
    _write(reg, '"coordinator.guard_level" = "strict"\n')
    assert _denies(_run(_kira_payload(tmp_path)))


def test_per_guard_strict_hardens_only_that_guard(reg, tmp_path):
    _write(
        reg,
        '"coordinator.guard_level" = "warn"\n'
        '"coordinator.guard_level.guard-kira-verdict-routed" = "strict"\n',
    )
    assert _denies(_run(_kira_payload(tmp_path)))
    other = mp.apply_guard_level(guard_manufactured_blocker.GUARD_NAME, deny("Stop", "x"))
    assert not _denies(other)


@pytest.mark.parametrize(
    "mod, name",
    [
        (guard_kira_verdict_routed, "guard-kira-verdict-routed"),
        (guard_manufactured_blocker, "guard-manufactured-blocker"),
        (guard_terminal_review, "guard-terminal-review"),
        (watchdog_undischarged_next_move, "watchdog-undischarged-next-move"),
    ],
)
def test_guard_name_constant(mod, name):
    assert mod.GUARD_NAME == name
