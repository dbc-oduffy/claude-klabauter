
from __future__ import annotations

import os

from coordinator_core.session import core
from coordinator_core.warm.entry_seam import per_request_state

_CALLER = "a73e6ebf-3a04-472c-80f6-5b38c7cd9889"
_SPAWNER = "8f4cecbf-8ae6-4be9-bb3a-c7aa1b0a63d2"


def _ids(env: dict) -> dict:
    return {k: env[k] for k in core.SESSION_ENV_PRECEDENCE if k in env}


def test_cold_carries_this_process_own_id(monkeypatch):
    for var in core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _CALLER)

    env = core.subprocess_identity_env()

    assert _ids(env) == {var: _CALLER for var in core.SESSION_ENV_PRECEDENCE}


def test_warm_carries_the_caller_not_the_servers_spawner(monkeypatch):
    for var in core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SPAWNER)

    with per_request_state(session_id=_CALLER, warm_served=True, isolated=True):
        env = core.subprocess_identity_env()

    assert _ids(env) == {var: _CALLER for var in core.SESSION_ENV_PRECEDENCE}
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
