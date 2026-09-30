"""Warn-and-pass leg of ``guard_doctrine_surface_bash_write``: a Bash-path write
to a CLAUDE.md-class surface advises (never denies) while the approval gate is off."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard
from coordinator_core.write_guards._guard_level import DOCTRINE_SURFACE_ADVISORY

SURFACES = [
    "global-doctrine/CLAUDE.md",
    "CLAUDE.md",
    "coordinator/snippets/em-operating-doctrine.md",
    "coordinator/snippets/agent-role-dispatched.md",
]

HEREDOC = (
    "cd /home/user/coordinator-content-repo && python3 - <<'EOF'\n"
    "p='global-doctrine/CLAUDE.md'; s=open(p).read(); s=s.replace('a','b'); "
    "open(p,'w').write(s)\nEOF"
)


@pytest.fixture(autouse=True)
def _gate_off(monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE", "off")


def _run(command, tool="Bash"):
    return guard.check({"tool_name": tool, "tool_input": {"command": command}}, SURFACES)


def _text(result):
    hso = result["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    return hso["additionalContext"]


@pytest.mark.parametrize(
    "command",
    [
        HEREDOC,
        "python3 -c \"open('global-doctrine/CLAUDE.md','w').write('x')\"",
        "sed -i s/a/b/ global-doctrine/CLAUDE.md",
        "echo x >> ~/.claude/rules/context7.md",
        "tee coordinator/snippets/em-operating-doctrine.md < /tmp/x",
    ],
)
def test_bash_write_to_a_doctrine_surface_warns(command):
    assert _text(_run(command)) == DOCTRINE_SURFACE_ADVISORY


def test_powershell_dialect_warns():
    assert _run("Set-Content global-doctrine/CLAUDE.md 'x'", tool="PowerShell") is not None


@pytest.mark.parametrize(
    "command",
    [
        "cat global-doctrine/CLAUDE.md",
        "ls ~/.claude/rules/",
        "echo hi > /tmp/scratch.txt",
        "echo x > coordinator.local.md",
    ],
)
def test_reads_and_unrelated_writes_pass_silently(command):
    assert _run(command) is None


def test_advisory_is_about_two_lines():
    assert len(DOCTRINE_SURFACE_ADVISORY.splitlines()) == 2


def test_gate_on_keeps_the_hard_deny(monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE", "on")
    hso = _run(HEREDOC)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_any_repos_existing_claude_md_warns(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("x")
    assert _text(_run(f"echo x >> {target}")) == DOCTRINE_SURFACE_ADVISORY
