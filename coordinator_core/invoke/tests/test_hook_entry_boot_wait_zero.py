"""The hook/cold-guard entry never sleeps on the warm-respawn boot wait.

Keyed on the entry itself (`hooks.*` method or `COORDINATOR_DOOR_STDIN_MODE=hook`), so a door that
exports no `COORDINATOR_WARM_BOOT_WAIT_SECS` still gets a zero wait. The op door keeps its wait, and a
cold fall-through on a warm miss still succeeds (DR-367).
"""

from __future__ import annotations

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.invoke.__main__ import _dispatch_argv, _warm_boot_wait_deadline

pytestmark = pytest.mark.complete

OP = "test.hook_entry_boot_wait_zero_ping"


@pytest.fixture(autouse=True)
def _registered_ping_op():
    saved = ipc._REGISTRY.get(OP)
    ipc._REGISTRY[OP] = lambda params, ctx=None, repo_root=None: {"pong": True}
    yield
    if saved is None:
        ipc._REGISTRY.pop(OP, None)
    else:
        ipc._REGISTRY[OP] = saved


def _miss_everywhere(monkeypatch, polls: list) -> None:
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", False)
    monkeypatch.delenv(ipc.PYTEST_UNSTAMPED_DISPATCH_ENV, raising=False)
    monkeypatch.setattr(ipc, "_is_dispatch_engine_stamped", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.settings.is_warm_enabled", lambda: True)
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)
    monkeypatch.setattr(
        "coordinator_core.warm.client.try_warm_dispatch",
        lambda msg, **kw: polls.append(msg) or None,
    )
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.record_client_boot_wait", lambda **kw: None
    )


def test_hook_mode_entry_runs_cold_at_once_with_a_nonzero_boot_wait_env(monkeypatch, tmp_path):
    polls: list = []
    _miss_everywhere(monkeypatch, polls)
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "60")
    monkeypatch.setenv("COORDINATOR_DOOR_STDIN_MODE", "hook")
    sleeps: list = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    stdout, stderr, code = _dispatch_argv([OP, "{}"], str(tmp_path), allow_warm=True)

    assert code == 0, stderr
    assert '"pong":true' in stdout.lower().replace(" ", "")
    assert "waiting up to" not in stderr
    assert sleeps == []
    assert len(polls) == 1, "the one miss, no poll loop"


def test_op_door_without_hook_mode_still_waits(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DOOR_STDIN_MODE", raising=False)
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0.3")
    polls: list = []
    _miss_everywhere(monkeypatch, polls)
    monkeypatch.setattr("coordinator_core.invoke.warm_miss._BOOT_POLL_MIN_SECS", 0.01)

    stdout, stderr, code = _dispatch_argv([OP, "{}"], str(tmp_path), allow_warm=True)

    assert code == 0, stderr
    assert "waiting up to" in stderr
    assert len(polls) > 1


def test_deadline_is_zero_for_hook_methods_and_hook_mode_only(monkeypatch):
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "5")
    monkeypatch.delenv("COORDINATOR_DOOR_STDIN_MODE", raising=False)
    assert _warm_boot_wait_deadline("hooks.stop_dispatch") == 0.0
    assert _warm_boot_wait_deadline("invoke.from_argv") == 5.0
    monkeypatch.setenv("COORDINATOR_DOOR_STDIN_MODE", "hook")
    assert _warm_boot_wait_deadline("invoke.from_argv") == 0.0
    monkeypatch.setenv("COORDINATOR_DOOR_STDIN_MODE", "other")
    assert _warm_boot_wait_deadline("invoke.from_argv") == 5.0
