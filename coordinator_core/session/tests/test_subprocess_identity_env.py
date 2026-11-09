
from __future__ import annotations

import os
import pytest

from coordinator_core.session import core
from coordinator_core.warm.entry_seam import per_request_state

_CALLER = "a73e6ebf-3a04-472c-80f6-5b38c7cd9889"
_SPAWNER = "8f4cecbf-8ae6-4be9-bb3a-c7aa1b0a63d2"


@pytest.fixture(autouse=True)
def clean_identity(monkeypatch):
    for var in core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COORDINATOR_AGENT_PROVIDER", "claude")


def _ids(env: dict) -> dict:
    return {k: env[k] for k in core.SESSION_ENV_PRECEDENCE if k in env}


def test_cold_carries_this_process_own_id(monkeypatch):
    for var in core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _CALLER)

    env = core.subprocess_identity_env()

    assert _ids(env) == {var: _CALLER for var in core.CLAUDE_SESSION_ENV_PRECEDENCE}


def test_warm_carries_the_caller_not_the_servers_spawner(monkeypatch):
    for var in core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SPAWNER)

    with per_request_state(session_id=_CALLER, warm_served=True, isolated=True):
        env = core.subprocess_identity_env()

    assert _ids(env) == {var: _CALLER for var in core.CLAUDE_SESSION_ENV_PRECEDENCE}
    assert _SPAWNER not in env.values()


def test_warm_with_no_carried_identity_strips_rather_than_inherits(monkeypatch):
    for var in core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SPAWNER)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SPAWNER)

    with per_request_state(warm_served=True, isolated=True):
        env = core.subprocess_identity_env()

    assert _ids(env) == {}


def test_non_identity_environment_is_carried_through(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _CALLER)
    monkeypatch.setenv("COORDINATOR_TEST_PASSTHROUGH", "kept")

    env = core.subprocess_identity_env()

    assert env["COORDINATOR_TEST_PASSTHROUGH"] == "kept"


def test_never_mutates_the_process_environment(monkeypatch):
    for var in core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SPAWNER)
    before = dict(os.environ)

    with per_request_state(session_id=_CALLER, warm_served=True, isolated=True):
        core.subprocess_identity_env()

    assert dict(os.environ) == before


def test_explicit_base_is_used_and_not_mutated(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _CALLER)
    base = {"PATH": "/nowhere", "COORDINATOR_SESSION_ID": _SPAWNER}

    env = core.subprocess_identity_env(base)

    assert env["PATH"] == "/nowhere"
    assert env["COORDINATOR_SESSION_ID"] == _CALLER
    assert base["COORDINATOR_SESSION_ID"] == _SPAWNER


def test_codex_thread_identity_and_child_provenance(monkeypatch):
    monkeypatch.delenv("COORDINATOR_AGENT_PROVIDER", raising=False)
    monkeypatch.setenv("CODEX_THREAD_ID", _CALLER)
    assert core.resolve_session_id() == _CALLER
    env = core.subprocess_identity_env()
    assert env["CODEX_THREAD_ID"] == _CALLER
    assert env["COORDINATOR_AGENT_PROVIDER"] == "codex"


def test_claude_children_do_not_acquire_codex_marker(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _CALLER)
    env = core.subprocess_identity_env({"CODEX_THREAD_ID": _SPAWNER})
    assert "CODEX_THREAD_ID" not in env
    assert env["COORDINATOR_AGENT_PROVIDER"] == "claude"


def test_unisolated_warm_child_replaces_daemon_role_model_and_delegation(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_ROLE", "em")
    monkeypatch.setenv("COORDINATOR_AGENT_MODEL", "daemon-model")
    monkeypatch.setenv("COORDINATOR_ALLOW_FIRE_TOKEN", "daemon-token")
    caller = {"CODEX_THREAD_ID": _CALLER, "COORDINATOR_AGENT_VENDOR": "codex",
              "COORDINATOR_AGENT_ROLE": "worker", "COORDINATOR_AGENT_MODEL": "caller-model"}
    with per_request_state(session_id=_CALLER, env=caller, warm_served=True, isolated=False):
        env = core.subprocess_identity_env()
    assert env["COORDINATOR_AGENT_ROLE"] == "worker"
    assert env["COORDINATOR_AGENT_MODEL"] == "caller-model"
    assert env["COORDINATOR_AGENT_VENDOR"] == env["COORDINATOR_AGENT_PROVIDER"] == "codex"
    assert env["CODEX_THREAD_ID"] == _CALLER
    assert "COORDINATOR_ALLOW_FIRE_TOKEN" not in env


def test_unknown_warm_child_cannot_inherit_em_identity(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_ROLE", "em")
    monkeypatch.setenv("COORDINATOR_AGENT_VENDOR", "claude")
    with per_request_state(warm_served=True, isolated=False):
        env = core.subprocess_identity_env()
    assert env["COORDINATOR_AGENT_PROVIDER"] == "unknown"
    assert "COORDINATOR_AGENT_VENDOR" not in env
    assert "COORDINATOR_AGENT_ROLE" not in env
