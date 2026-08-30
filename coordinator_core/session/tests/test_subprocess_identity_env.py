"""`session.core.subprocess_identity_env` — the identity a CHILD PROCESS runs under.

THE DEFECT THIS CLOSES. A process boundary drops both sources that make
`attributable_session_id` correct under a warm dispatch: the tier-0 identity
ContextVar and the warm-served flag are process-local. What a child DOES
inherit is `os.environ`, which inside the resident server names whoever spawned
that server. Measured live 2026-08-30: `coordinator-lesson-add`, running
in-process inside a warm-served `workstream-complete-assemble apply`, spawned
`coordinator-queue-append` with the inherited environment; the child wrote
`state/lessons/...yaml` and touch-recorded it under a live peer, after which the
strict-scope guard refused the author's own commit on a provably-foreign owner
(`state/bug-backlog/2026-08-30-the-warm-engine-touch-records-a-session-
9c5555208afd.yaml`).

The stripping case below is the load-bearing one, and it is not symmetry for its
own sake: leaving the vars in place when nothing resolves hands the child the
stranger, which IS the defect. An absent claim is recoverable; a claim filed
under a live peer blocks that peer and the author both.
"""

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
    """The live shape: the server's env names a peer, the request names its caller."""
    for var in core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SPAWNER)

    with per_request_state(session_id=_CALLER, warm_served=True, isolated=True):
        env = core.subprocess_identity_env()

    assert _ids(env) == {var: _CALLER for var in core.SESSION_ENV_PRECEDENCE}
    assert _SPAWNER not in env.values()


def test_warm_with_no_carried_identity_strips_rather_than_inherits(monkeypatch):
    """A door that sent no identity must not promote the server's spawner.

    This is the exact case the backlog measured. The child resolves nothing and
    declines to claim — under-declaration, never a false claim, the failure
    direction `ipc._record_self_reported_touches` already chose.
    """
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
    """Concurrent requests share one warm server's `os.environ`.

    `contract.apply_base._mirror_session_env_for_subprocess` mutates and restores
    the real environment around one spawn; that shape is sound only in a
    single-request process and must not be copied here.
    """
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
