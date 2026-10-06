"""Boundary pin: the destructive-action guard allows the `grind-row undo` verb command and still
denies the raw restore/delete shapes the verb replaces, in both command dialects."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.contract.grind_vocab import encode_undo_paths

_AGENT_TYPE = "coordinator:queue-grind-op-runner"
_BIN = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/backlog-grind-assemble"'


def _payload(tool_name, command):
    return {
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": None,
        "agent_id": "deadbeef0123",
        "agent_type": _AGENT_TYPE,
    }


def _verb_command(paths):
    token = encode_undo_paths(paths)
    return f"{_BIN} grind-row undo --paths-urlenc '{token}' --repo-root /repo"


@pytest.mark.parametrize("tool_name", COMMAND_TOOL_NAMES)
@pytest.mark.parametrize(
    "paths",
    [
        ["src/a.py"],
        ["docs/git notes.md"],
        ["src/rm_util.py"],
        ["it's/odd.md", "docs/git notes.md", "src/rm_util.py"],
    ],
)
def test_verb_command_allows(tool_name, paths):
    assert guard.check(_payload(tool_name, _verb_command(paths))) is None


_GIT_SHAPES = [
    "git checkout HEAD -- unclaimed/file.py",
    "git checkout -- unclaimed/file.py",
    "git restore --source=HEAD unclaimed/file.py",
]
# PowerShell's `rm` is an alias whose `-f` is not the `-Force` switch, so the
# dialect-native force-delete shape is pinned for that tool name.
_RM_SHAPE = {"Bash": "rm -f unclaimed/file.py", "PowerShell": "Remove-Item -Force unclaimed/file.py"}


@pytest.mark.parametrize(
    ("tool_name", "command"),
    [(t, c) for t in COMMAND_TOOL_NAMES for c in (*_GIT_SHAPES, _RM_SHAPE[t])],
)
def test_raw_restore_shapes_deny(tool_name, command):
    result = guard.check(_payload(tool_name, command))
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
