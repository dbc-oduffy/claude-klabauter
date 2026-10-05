"""block_perforce_submit: the shell route never writes to Perforce on an armed box."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_perforce_submit as guard


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
    out = guard.check(_bash(cmd))
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


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
    assert guard.check(_bash(cmd)) is None


@pytest.mark.parametrize("value", [None, [], ""])
def test_submit_allowed_when_key_absent_or_empty(monkeypatch, tmp_path, value):
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_dir", lambda: tmp_path)
    flat = {} if value is None else {guard.POLICY_KEY: value}
    monkeypatch.setattr(machine_resolver, "load_flat_registry_file", lambda p: flat)
    assert guard.check(_bash("p4 submit -d x")) is None


def test_unreadable_registry_fails_closed(monkeypatch):
    from coordinator_core import machine_resolver

    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(machine_resolver, "registry_dir", boom)
    assert guard.check(_bash("p4 submit -d x")) is not None
