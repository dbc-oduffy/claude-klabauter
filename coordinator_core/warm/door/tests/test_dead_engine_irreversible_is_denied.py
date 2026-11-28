"""With no engine verdict, an irreversible-shaped command is denied naming the command; a recoverable one passes."""

from __future__ import annotations

import json

import pytest

from coordinator_core.warm.door.tests.test_cold_leg_kill_telemetry import (  # noqa: F401 -- fixtures
    _HANG,
    _Harness,
    door,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_PUSH = "git push --force origin main"


@pytest.fixture
def dead_engine(door, tmp_path, monkeypatch):
    harness = _Harness(door, tmp_path, _HANG, monkeypatch)
    yield harness
    harness.close()


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_an_unrun_irreversible_command_is_denied_naming_the_command_and_logged_once(dead_engine, tool):
    proc = dead_engine.hook(_PUSH, tool)
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout.decode("utf-8", "replace"))["hookSpecificOutput"]
    assert body["permissionDecision"] == "deny"
    assert "DENIED" in body["permissionDecisionReason"]
    assert _PUSH in body["permissionDecisionReason"]
    assert "cold guard gave no verdict" in body["permissionDecisionReason"]
    rows = dead_engine.rows()
    assert len(rows) == 1, rows
    assert rows[0]["cause"].endswith("irreversible_shaped=true")


def test_a_native_exact_row_still_denies_with_no_row(dead_engine):
    out = dead_engine.hook("git worktree add ../x").stdout.decode("utf-8", "replace")
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "BLOCKED by block-worktree-creation" in out
    assert dead_engine.rows() == []


def test_a_command_that_is_not_irreversible_shaped_is_not_denied(dead_engine):
    out = dead_engine.hook("echo hi").stdout.decode("utf-8", "replace")
    assert "DENIED" not in out
    assert '"deny"' not in out
