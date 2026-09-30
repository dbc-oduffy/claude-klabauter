"""A publish round is moved to a background task; nothing else is."""
import pytest

from coordinator_core.bash_guards.guard_background_publish import check_background_publish


def _payload(cmd, tool="Bash", **extra):
    return {"tool_name": tool, "tool_input": {"command": cmd, "description": "d", **extra}}


@pytest.mark.parametrize("cmd", [
    'python3 "$_mk/coordinator/bin/percolate-round.py" claude-klabauter --no-publish',
    "python3 coordinator/bin/publish.py claude-klabauter --no-commit > out.txt 2>&1",
    '"$_py" "$_sh/bin/percolate-round" coordinator-claude',
    "percolate-push claude-klabauter",
    r'python "$mk\coordinator\bin\percolate-round.py" x',
    "cd /x && timeout 550 python3 coordinator/bin/publish.py t --no-commit",
    "S=/tmp/s\npython3 coordinator/bin/percolate-round.py t > $S/r.txt 2>&1",
])
def test_publish_cli_is_backgrounded_with_input_preserved(cmd):
    out = check_background_publish(_payload(cmd, timeout=400000))
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated == {"command": cmd, "description": "d", "timeout": 400000, "run_in_background": True}
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_powershell_tool_is_covered():
    assert check_background_publish(_payload("python x\\percolate-round.py t", tool="PowerShell"))


@pytest.mark.parametrize("cmd", [
    "grep -n publish coordinator/bin/publish.pyc",
    "python3 coordinator/bin/publish-allowlist-generate.py",
    "python3 coordinator/bin/percolate-gate.py branch0-gate claude-klabauter",
    "cat docs/percolate-round-notes.md",
    "git status",
    "grep -n gap coordinator/bin/percolate-round.py | head -3",
    "sed -n 1,5p coordinator/bin/publish.py",
    "python3 - <<'EOF'\nopen('coordinator/bin/publish.py')\nEOF",
])
def test_non_publish_commands_are_untouched(cmd):
    assert check_background_publish(_payload(cmd)) is None


def test_already_backgrounded_is_untouched():
    assert check_background_publish(_payload("percolate-push t", run_in_background=True)) is None


def test_other_tools_are_untouched():
    assert check_background_publish(_payload("percolate-push t", tool="Read")) is None
