"""register_current resolves the Codex ancestor from `host_pid` and classifies against `env`, not the engine's own."""
from types import SimpleNamespace

import pytest

from coordinator_core.session import external_registry as registry

SID = "00000000-0000-4000-8000-0000000000bb"
PARENT = "00000000-0000-4000-8000-0000000000cc"


def _proc(name, cmdline=(), pid=123):
    return SimpleNamespace(pid=pid, name=lambda: name, cmdline=lambda: list(cmdline), create_time=lambda: 456)


def _register(tmp_path, monkeypatch, **kwargs):
    import psutil
    seen = []

    def fake_process(pid):
        seen.append(pid)
        return SimpleNamespace(parents=lambda: [_proc("zsh"), _proc("codex", ["codex"], pid=77)])

    monkeypatch.setattr(psutil, "Process", fake_process)
    cwd = tmp_path / "repo"
    cwd.mkdir(exist_ok=True)
    payload = {"session_id": SID, "cwd": str(cwd), "hook_event_name": "SessionStart"}
    return registry.register_current(payload, tmp_path, **kwargs), seen


def test_host_pid_selects_the_process_whose_ancestors_are_searched(tmp_path, monkeypatch) -> None:
    record, seen = _register(tmp_path, monkeypatch, host_pid=4242, env={})
    assert seen == [4242]
    assert record["pid"] == 77


def test_default_host_pid_is_this_process(tmp_path, monkeypatch) -> None:
    import os
    _, seen = _register(tmp_path, monkeypatch)
    assert seen == [os.getpid()]


def test_env_argument_drives_role_and_parent_not_os_environ(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("COORDINATOR_AGENT_ROLE", "worker")
    monkeypatch.setenv("COORDINATOR_PARENT_SESSION_ID", "00000000-0000-4000-8000-0000000000dd")
    record, _ = _register(
        tmp_path, monkeypatch, host_pid=1,
        env={"COORDINATOR_AGENT_ROLE": "reviewer", "COORDINATOR_PARENT_SESSION_ID": PARENT})
    assert record["role"] == "reviewer"
    assert record["parent_session_id"] == PARENT


def test_host_pid_and_env_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        registry.register_current({}, None, 1)  # type: ignore[misc]
