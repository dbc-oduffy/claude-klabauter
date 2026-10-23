"""block_perforce_submit: the shell route never writes to Perforce on an armed box."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_perforce_submit as guard
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


def _bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setattr(guard, "_armed", lambda: True)


@pytest.mark.parametrize(
    "cmd",
    [
        "p4 submit -d x",
        "p4 shelve -c 123",
        "p4 -p perforce.example-fleet-sports.xyz:1666 submit",
        "git p4 submit",
        "git-p4 submit",
        "p4 $VERB -d x",
        "cd src && p4 reshelve -s 1 -c 2",
    ],
)
def test_server_writes_denied_when_armed(armed, cmd):
    out = _enveloped(guard.check)(_bash(cmd))
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "tool,cmd",
    [
        ("Bash", "p4 submit"),
        ("Bash", "p4 submit -c 123"),
        ("Bash", "p4 -c ws -u me submit"),
        ("Bash", "p4 shelve -c 123"),
        ("Bash", "p4 edit f && p4 submit"),
        ("PowerShell", "& p4.exe submit"),
    ],
)
def test_deny_reason_names_anchor_and_wiki_path(armed, tool, cmd):
    out = _enveloped(guard.check)({"tool_name": tool, "tool_input": {"command": cmd}})
    assert out is not None
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "P4-SUBMIT-AND-SHELVE-ARE-DENIED-ON-A-NO-SUBMIT-BOX" in reason


@pytest.mark.parametrize(
    "cmd",
    [
        "p4 edit a.cpp",
        "p4 sync //depot/...",
        "p4 revert a.cpp",
        "p4 unshelve -s 123",
        "p4 opened",
        'echo "p4 submit"',
        'grep "p4 submit" notes.txt',
    ],
)
def test_local_and_mention_only_allowed_when_armed(armed, cmd):
    assert _enveloped(guard.check)(_bash(cmd)) is None


@pytest.mark.parametrize("value", [None, [], ""])
def test_submit_allowed_when_key_absent_or_empty(monkeypatch, tmp_path, value):
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_dir", lambda: tmp_path)
    flat = {} if value is None else {guard.POLICY_KEY: value}
    monkeypatch.setattr(machine_resolver, "load_flat_registry_file", lambda p: flat)
    assert _enveloped(guard.check)(_bash("p4 submit -d x")) is None


def test_unreadable_registry_fails_closed(monkeypatch):
    from coordinator_core import machine_resolver

    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(machine_resolver, "registry_dir", boom)
    assert _enveloped(guard.check)(_bash("p4 submit -d x")) is not None


def _registry(monkeypatch, tmp_path, *, vcs=None, policy=None):
    from coordinator_core import machine_resolver

    reg = tmp_path / "reg"
    reg.mkdir()
    lines = []
    if vcs is not None:
        ws = tmp_path / "ws"
        ws.mkdir()
        lines += [
            "[external_workspaces.ws]",
            f"path = '{ws.as_posix()}'",
            f'vcs = "{vcs}"',
        ]
    if policy is not None:
        lines += ["[policy]", f"p4_no_submit = {policy}"]
    (reg / "registry.local.toml").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(machine_resolver, "registry_dir", lambda: reg)


@pytest.mark.parametrize("cmd", ["p4 submit -d x", "p4 shelve -c 5"])
def test_armed_by_p4_workspace_alone(monkeypatch, tmp_path, cmd):
    _registry(monkeypatch, tmp_path, vcs="P4")
    assert _enveloped(guard.check)(_bash(cmd)) is not None


def test_armed_by_policy_key_alone(monkeypatch, tmp_path):
    _registry(monkeypatch, tmp_path, policy='["//depot"]')
    assert _enveloped(guard.check)(_bash("p4 submit -d x")) is not None


def test_unarmed_with_neither(monkeypatch, tmp_path):
    _registry(monkeypatch, tmp_path)
    assert _enveloped(guard.check)(_bash("p4 submit -d x")) is None


def test_git_workspace_alone_does_not_arm(monkeypatch, tmp_path):
    _registry(monkeypatch, tmp_path, vcs="git")
    assert _enveloped(guard.check)(_bash("p4 submit -d x")) is None


def test_empty_policy_key_with_p4_workspace_stays_armed(monkeypatch, tmp_path):
    _registry(monkeypatch, tmp_path, vcs="p4", policy="[]")
    assert _enveloped(guard.check)(_bash("p4 submit -d x")) is not None


def test_p4_workspace_still_allows_local_verbs(monkeypatch, tmp_path):
    _registry(monkeypatch, tmp_path, vcs="p4")
    assert _enveloped(guard.check)(_bash("p4 sync //depot/...")) is None
