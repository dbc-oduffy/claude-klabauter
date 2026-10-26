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
