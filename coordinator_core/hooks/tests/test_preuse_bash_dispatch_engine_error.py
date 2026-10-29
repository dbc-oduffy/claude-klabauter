"""preuse_bash_dispatch: an exception from evaluate_payload_json denies the closed irreversible set, advises on the rest."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.hooks import preuse_bash_dispatch as mod

_ERR = "boom-evaluate"


@pytest.fixture
def broken_evaluate(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError(_ERR)

    monkeypatch.setattr(dispatch, "evaluate_payload_json", boom)


def _run(command: str) -> dict:
    return mod._handler({"payload": {"tool_name": "Bash", "tool_input": {"command": command}}})


@pytest.mark.parametrize(
    "command",
    ["git push origin main", "p4 submit", "git add x && git push", "gh pr merge 3", "git stash drop"],
)
def test_irreversible_denied_on_engine_error(broken_evaluate, command):
    hso = _run(command)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert _ERR in hso["permissionDecisionReason"]


@pytest.mark.parametrize("command", ["git status", "git commit -m y", "git branch -d foo", "ls"])
def test_other_commands_get_advisory_on_engine_error(broken_evaluate, command):
    hso = _run(command)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "could not be evaluated" in hso["additionalContext"]
