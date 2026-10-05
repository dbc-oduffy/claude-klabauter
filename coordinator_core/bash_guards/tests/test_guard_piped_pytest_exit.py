"""Tests for guard_piped_pytest_exit: advisory-only, both command tool names."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards.guard_piped_pytest_exit import check_piped_pytest_exit


def _ctx(out):
    return out["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    "cmd",
    [
        "python -m pytest x | tail -3",
        "python -m pytest x 2>&1 | grep FAILED",
        "pytest x | head -20",
        "py -m pytest x | Select-Object -Last 5",
        "python -m pytest x 2>&1 | Select-String FAILED",
    ],
)
@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_fires_advisory_only(cmd, tool_name):
    out = check_piped_pytest_exit(cmd, "s", payload={"tool_name": tool_name, "tool_input": {"command": cmd}})
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "updatedInput" not in hso
    assert "PIPED-EXIT-CODE-IS-THE-PIPES" in _ctx(out)
    assert "`$LASTEXITCODE`" in _ctx(out)


@pytest.mark.parametrize(
    "cmd",
    [
        "python -m pytest x > log; tail log",
        "set -o pipefail; python -m pytest x | tail -3",
        "pytest x | tail; echo ${PIPESTATUS[0]}",
        "python -m pytest x; $LASTEXITCODE | Select-Object -First 1",
        "git log | head",
        "git commit -m 'pytest | tail'",
        "pytest x; ls | head",
        "",
    ],
)
@pytest.mark.parametrize("tool_name", ["Bash", "PowerShell"])
def test_silent(cmd, tool_name):
    payload = {"tool_name": tool_name, "tool_input": {"command": cmd}}
    assert check_piped_pytest_exit(cmd, "s", payload=payload) is None
