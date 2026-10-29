"""A payload with no `agent_id` key: main thread only when it names no subagent type.

Pins both the bash and powershell legs: a payload carrying `agent_type` or
`subagent_type` and no `agent_id` is classified (never allowed as main thread),
and the genuine main-thread allow logs `main-thread-out-of-scope`, not FAIL-OPEN.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.guard_messages.testing import envelope_of

LEGS = [
    ("Bash", "git reset --hard HEAD~1"),
    ("PowerShell", "git reset --hard HEAD~1"),
]


def _payload(tool, command, **extra):
    p = {
        "tool_name": tool,
        "tool_input": {"command": command},
        "session_id": "sess-no-agent-id",
        "cwd": None,
    }
    p.update(extra)
    return p


@pytest.mark.parametrize("tool,command", LEGS)
def test_no_identity_keys_is_main_thread_and_logs_new_label(tool, command, tmp_path, monkeypatch):
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    assert envelope_of(guard.check(_payload(tool, command))) is None
    content = log_path.read_text(encoding="utf-8")
    assert "main-thread-out-of-scope" in content
    assert "FAIL-OPEN" not in content
    assert "branch='no-agent-id-key'" in content


@pytest.mark.parametrize("tool,command", LEGS)
def test_agent_type_without_agent_id_is_never_main_thread(tool, command, tmp_path, monkeypatch):
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    result = envelope_of(guard.check(_payload(tool, command, agent_type="coordinator:executor")))
    assert result is not None
    assert not log_path.exists() or "main-thread-out-of-scope" not in log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("tool,command", LEGS)
def test_subagent_type_without_agent_id_is_never_main_thread(tool, command, tmp_path, monkeypatch):
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    result = envelope_of(guard.check(_payload(tool, command, subagent_type="coordinator:executor")))
    assert result is not None
    assert not log_path.exists() or "main-thread-out-of-scope" not in log_path.read_text(encoding="utf-8")


def test_other_fail_open_sites_keep_their_label(tmp_path, monkeypatch):
    log_path = tmp_path / "fail-open.log"
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: log_path)
    guard._log_fail_open("kind-unresolved-unparseable-agent-id", {"session_id": "s1"}, "rm -rf /")
    assert " FAIL-OPEN " in log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_main_thread_pid_kill_not_denied(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: tmp_path / "fail-open.log")
    cmd = "kill -9 12345" if tool == "Bash" else "Stop-Process -Id 12345 -Force"
    assert envelope_of(guard.check(_payload(tool, cmd))) is None


EM_KILL_COMMANDS = [
    ("Bash", "kill 12345"),
    ("Bash", "kill -TERM 12345"),
    ("Bash", "kill -KILL 12345 12346"),
    ("Bash", "kill -9 1"),
    ("Bash", "sudo kill -9 12345"),
    ("Bash", "pkill -9 -P 1"),
    ("Bash", "taskkill /PID 12345 /F"),
    ("Bash", "taskkill //PID 12345 //F"),
    ("PowerShell", "Stop-Process -Id 12345 -Force"),
    ("PowerShell", "Stop-Process -Id 12345,12346"),
    ("PowerShell", "taskkill /PID 12345 /F"),
    ("PowerShell", "kill 12345"),
]


@pytest.mark.parametrize("tool,command", EM_KILL_COMMANDS)
def test_em_numeric_kill_verbs_never_denied(tool, command, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: tmp_path / "fail-open.log")
    assert envelope_of(guard.check(_payload(tool, command))) is None


@pytest.mark.parametrize("tool,command", [
    ("Bash", "kill 12345"),
    ("PowerShell", "Stop-Process -Id 12345 -Force"),
])
def test_em_orphan_kill_never_denied_even_when_ownership_unprovable(tool, command, tmp_path, monkeypatch):
    """An orphan reparented to ppid 1 has no ledger entry; the EM kill is still allowed."""
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: tmp_path / "fail-open.log")
    monkeypatch.setattr(guard, "_all_pids_owned_by_agent", lambda *a, **k: False)
    assert envelope_of(guard.check(_payload(tool, command))) is None


def test_subagent_kill_denial_names_em_route(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "_fail_open_log_path", lambda: tmp_path / "fail-open.log")
    result = envelope_of(guard.check(_payload("Bash", "kill 12345", agent_type="coordinator:executor")))
    assert result is not None
    assert "EM runs the kill" in str(result)
