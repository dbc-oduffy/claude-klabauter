"""preuse_bash_dispatch: guard-import failure denies a closed irreversible set, allows the rest."""

from __future__ import annotations

import builtins

import pytest

from coordinator_core.hooks import preuse_bash_dispatch as mod

_ERR = "boom-guard-import"


@pytest.fixture
def broken_import(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name == "coordinator_core.bash_guards.dispatch":
            raise ImportError(_ERR)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)


def _run(command: str) -> dict:
    return mod._handler({"payload": {"tool_name": "Bash", "tool_input": {"command": command}}})


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "p4 submit",
        "git add x && git push",
        "git -C repo push --force",
        "P4 shelve -c 5",
        "gh pr merge 3",
        "git branch -D foo",
        "git stash drop",
        "git stash clear",
    ],
)
def test_irreversible_denied(broken_import, command):
    hso = _run(command)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert _ERR in hso["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    ["git status", "git add x", "git commit -m y", "git branch -d foo", "ls"],
)
def test_other_commands_get_advisory(broken_import, command):
    hso = _run(command)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "could not be evaluated" in hso["additionalContext"]
