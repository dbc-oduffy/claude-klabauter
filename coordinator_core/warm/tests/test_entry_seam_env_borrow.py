"""`entry_seam.per_request_state`'s `os.environ` identity borrow (C3).

Purpose: docs/plans/2026-08-30-every-op-runs-in-the-callers-environment.md
task C3. `session_identity_override` binds a ContextVar only, so a raw
`os.environ.get(...)` read (harness_registry.self_record()'s `CLAUDE_PID`
leg among them) steps past it and reads whoever spawned the server. This
pins the four behaviours `isolated` gates, and that restore actually
unwinds -- the spike behind this chunk measured 36 of 40 pool tasks
observing a prior task's unrestored value.

Negative-spec (RAG-bait):
    Does not exercise a live warm server, a real process pool, or the wire
    (`warm.client`/`_serve_line`) -- those are `test_server_loop.py`'s job.
    This file pins `per_request_state`'s own `os.environ` contract in
    isolation, against the real function, no stand-in.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.warm.entry_seam import per_request_state

_CALLER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_SPAWNER_COORDINATOR = "11111111-1111-4111-8111-111111111111"
_SPAWNER_CLAUDE = "22222222-2222-4222-8222-222222222222"
_SPAWNER_CLAUDE_CODE = "33333333-3333-4333-8333-333333333333"

_SESSION_NAMES = ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID")


def _set_spawner_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SPAWNER_COORDINATOR)
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SPAWNER_CLAUDE)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SPAWNER_CLAUDE_CODE)
    monkeypatch.setenv("CLAUDE_PID", "424242")


def test_carried_identity_isolated_sets_top_tier_and_pops_lower_tiers_and_pid(monkeypatch):
    """The decisive production case: `_pool_dispatch_worker`'s own binding."""
    _set_spawner_env(monkeypatch)

    with per_request_state(session_id=_CALLER, warm_served=True, isolated=True):
        assert os.environ["COORDINATOR_SESSION_ID"] == _CALLER
        assert "CLAUDE_SESSION_ID" not in os.environ
        assert "CLAUDE_CODE_SESSION_ID" not in os.environ
        assert "CLAUDE_PID" not in os.environ

    # Restored, byte-for-byte, once the block closes.
    assert os.environ["COORDINATOR_SESSION_ID"] == _SPAWNER_COORDINATOR
    assert os.environ["CLAUDE_SESSION_ID"] == _SPAWNER_CLAUDE
    assert os.environ["CLAUDE_CODE_SESSION_ID"] == _SPAWNER_CLAUDE_CODE
    assert os.environ["CLAUDE_PID"] == "424242"


def test_no_carried_identity_isolated_strips_every_name(monkeypatch):
    """Warm-served, isolated, door sent no identity: absent, never the
    owner's — `session.core.carried_session_id()`'s omit-never-substitute
    contract, mirrored onto the env axis."""
    _set_spawner_env(monkeypatch)

    with per_request_state(warm_served=True, isolated=True):
        for name in _SESSION_NAMES:
            assert name not in os.environ
        assert "CLAUDE_PID" not in os.environ

    for name in _SESSION_NAMES:
        assert os.environ[name]
    assert os.environ["CLAUDE_PID"] == "424242"


def test_not_isolated_leaves_os_environ_untouched(monkeypatch):
    """The `BrokenProcessPool` fallback's own disposition: a carried identity
    still binds the ContextVar (not asserted here — this pins the env axis
    only), but `os.environ` -- shared with every other in-flight connection
    on this leg -- must never be written or popped."""
    _set_spawner_env(monkeypatch)
    before = dict(os.environ)

    with per_request_state(session_id=_CALLER, warm_served=True, isolated=False):
        assert dict(os.environ) == before

    assert dict(os.environ) == before


def test_non_uuid_session_id_isolated_treated_as_no_carried_identity(monkeypatch):
    """A malformed value crossing the wire must never be trusted onto the
    env axis either -- same fail-safe direction as `session_identity_
    override`'s own gate."""
    _set_spawner_env(monkeypatch)

    with per_request_state(session_id="not-a-uuid", warm_served=True, isolated=True):
        for name in _SESSION_NAMES:
            assert name not in os.environ
        assert "CLAUDE_PID" not in os.environ

    for name in _SESSION_NAMES:
        assert os.environ[name]


def test_restore_unwinds_even_when_the_block_raises(monkeypatch):
    _set_spawner_env(monkeypatch)

    with pytest.raises(RuntimeError):
        with per_request_state(session_id=_CALLER, warm_served=True, isolated=True):
            assert os.environ["COORDINATOR_SESSION_ID"] == _CALLER
            raise RuntimeError("boom")

    assert os.environ["COORDINATOR_SESSION_ID"] == _SPAWNER_COORDINATOR
    assert os.environ["CLAUDE_SESSION_ID"] == _SPAWNER_CLAUDE
    assert os.environ["CLAUDE_CODE_SESSION_ID"] == _SPAWNER_CLAUDE_CODE
    assert os.environ["CLAUDE_PID"] == "424242"


def test_isolated_is_a_required_keyword_only_argument():
    with pytest.raises(TypeError):
        per_request_state()  # type: ignore[call-arg]
