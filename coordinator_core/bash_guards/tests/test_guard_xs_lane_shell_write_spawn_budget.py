"""Zero subprocess spawns from `xs-lane-shell-write`, on a firing and a non-firing command. The guard
runs on every Bash PreToolUse, so a spawn per call is a process-budget defect; this counts
processes, not wall clock."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import guard_xs_lane_shell_write as guard
from coordinator_core.bash_guards.tests.test_guard_xs_lane_shell_write import (  # noqa: F401 -- fixture reused
    SID,
    _heredoc,
    _spend_the_budget,
    env,
)
from coordinator_core.session import xs_lane_budget as xb


@pytest.fixture
def spawns(monkeypatch):
    seen = []

    def forbid(*args, **kwargs):
        seen.append(args[0] if args else kwargs.get("args"))
        raise AssertionError("xs-lane-shell-write spawned a subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbid)
    monkeypatch.setattr(subprocess, "run", forbid)
    return seen


def test_firing_command_spawns_nothing(env, spawns):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    _spend_the_budget(root)
    record = guard.check(_heredoc(str(root / "pkg" / "mod.py")), SID, str(root), {"session_id": SID, "cwd": str(root)})
    assert record is not None and record.verdict_kind == "deny"
    assert spawns == []


def test_non_firing_command_spawns_nothing(env, spawns):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    assert guard.check("ls -la", SID, str(root), {"session_id": SID, "cwd": str(root)}) is None
    assert guard.check(_heredoc(str(root / "pkg" / "mod.py")), SID, str(root), {"session_id": SID, "cwd": str(root)}) is None
    assert spawns == []
