"""Sentinel-gated deny leg of ``guard_doctrine_surface_bash_write``: a Bash-path write
to a CLAUDE.md-class or rules surface is denied unless a fresh doctrine-edit sentinel is
present; the gate being off leaves the leg quiet."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard
from coordinator_core.write_guards._doctrine_edit_sentinel import DENY_REASON, SENTINEL_BASENAME
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))

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
def _gate_default(monkeypatch):
    from coordinator_core import machine_profile

    monkeypatch.delenv("MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE", raising=False)
    machine_profile.reset_cache()
    yield
    machine_profile.reset_cache()


@pytest.fixture()
def cwd(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _run(command, tool="Bash", cwd=None):
    payload = {"tool_name": tool, "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return _enveloped(guard.check)(payload, SURFACES)


def _denied(result):
    hso = result["hookSpecificOutput"]
    assert hso.get("permissionDecision") == "deny"
    return hso["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        HEREDOC,
        "python3 -c \"open('global-doctrine/CLAUDE.md','w').write('x')\"",
        "sed -i s/a/b/ global-doctrine/CLAUDE.md",
        "echo x >> ~/.claude/rules/context7.md",
    ],
)
def test_bash_write_to_a_doctrine_surface_is_denied(command, cwd):
    assert DENY_REASON in _denied(_run(command, cwd=cwd))


@pytest.mark.parametrize(
    "command",
    [
        "echo x >> ~/.claude/rules/context7.md",
        "sed -i s/a/b/ global-doctrine/rules/x.md",
        HEREDOC,
    ],
)
def test_fresh_sentinel_admits_the_write(command, cwd):
    (cwd / ".git" / SENTINEL_BASENAME).write_text("")
    assert _run(command, cwd=cwd) is None


def test_stale_sentinel_still_denies(cwd):
    import os
    import time

    sentinel = cwd / ".git" / SENTINEL_BASENAME
    sentinel.write_text("")
    old = time.time() - 7200
    os.utime(sentinel, (old, old))
    assert DENY_REASON in _denied(_run("echo x >> ~/.claude/rules/context7.md", cwd=cwd))


def test_deny_names_the_sentinel_and_offers_no_edit_tool(cwd):
    reason = _denied(_run("echo x >> ~/.claude/rules/context7.md", cwd=cwd))
    assert SENTINEL_BASENAME in reason
    assert "the Edit tool" not in reason


def test_powershell_dialect_denies(cwd):
    assert _run("Set-Content global-doctrine/CLAUDE.md 'x'", tool="PowerShell", cwd=cwd) is not None


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


def test_gate_on_denies_and_a_fresh_sentinel_admits(monkeypatch, cwd):
    from coordinator_core import machine_profile

    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE", "on")
    machine_profile.reset_cache()
    assert _denied(_run(HEREDOC, cwd=cwd))
    (cwd / ".git" / SENTINEL_BASENAME).write_text("")
    assert _run(HEREDOC, cwd=cwd) is None


def test_any_repos_existing_claude_md_is_denied(tmp_path, cwd):
    target = tmp_path / "CLAUDE.md"
    target.write_text("x")
    assert DENY_REASON in _denied(_run(f"echo x >> {target}", cwd=cwd))


def test_gate_off_leaves_the_write_quiet(monkeypatch, cwd):
    from coordinator_core import machine_profile

    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE", "off")
    machine_profile.reset_cache()
    assert _run(HEREDOC, cwd=cwd) is None


def test_governed_surface_outside_the_class_stays_advisory_and_ignores_the_sentinel(cwd):
    command = "tee coordinator/snippets/em-operating-doctrine.md < /tmp/x"
    hso = _run(command, cwd=cwd)["hookSpecificOutput"]
    assert hso.get("permissionDecision") != "deny"
    assert hso["additionalContext"]
    assert SENTINEL_BASENAME not in hso["additionalContext"]


def test_dispatched_caller_stays_denied_with_a_fresh_sentinel_and_is_not_told_of_it(cwd):
    (cwd / ".git" / SENTINEL_BASENAME).write_text("")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo x >> ~/.claude/rules/context7.md"},
        "cwd": str(cwd),
        "agent_id": "run-1",
        "agent_type": "executor",
    }
    reason = _denied(_enveloped(guard.check)(payload, SURFACES))
    assert SENTINEL_BASENAME not in reason
