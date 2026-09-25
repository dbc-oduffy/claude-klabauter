"""Regression pin for P088-C2 (docs/plans/2026-09-11-session-identity-residue-
memo-send-sent.md).

VERDICT, not a fix: `memo_send._resolve_sent_by`'s one ambient identity read
(`coordinator_core.ops.session_context.resolve_current_session_id`) already
delegates to `session.core.attributable_session_id`, which resolves tier-0
(`carried_session_id()`) only under a warm dispatch and the full env-tier
chain (`resolve_session_id(cwd)`) cold — the anti-forgery, warm-scoped policy
C2's original option (a) proposed adding was already live. This file pins
that behaviour rather than re-deriving it.

Three cases, per the plan row body:
  - warm-served with a carried id stamps that id.
  - warm-served with NO carried id stamps `_SENT_BY_UNRESOLVED`, never the
    ambient env value (the anti-forgery case).
  - COLD with only the env var set still stamps the env value — the
    regression pin for the ~49-memo incident this docstring's own history
    records; that incident is NOT reopened by this row.
"""

from __future__ import annotations

from coordinator_core.ops.fleet.memo_send import _SENT_BY_UNRESOLVED, _resolve_sent_by
from coordinator_core.session import core as session_core

# UUID-shaped: `session_identity_override` silently no-ops on anything that
# does not match `_UUID_RE`.
_CARRIED_SID = "cccccccc-3333-4333-8333-cccccccccccc"
_SERVER_OWNER_SID = "dddddddd-4444-4444-8444-dddddddddddd"


def _ambient_env_holds(monkeypatch, sid: str) -> None:
    for var in session_core.SESSION_ENV_PRECEDENCE:
        monkeypatch.setenv(var, sid)


def test_warm_with_carried_id_stamps_it(monkeypatch):
    _ambient_env_holds(monkeypatch, _SERVER_OWNER_SID)
    with session_core.warm_served_request():
        with session_core.session_identity_override(_CARRIED_SID):
            assert _resolve_sent_by({}) == _CARRIED_SID


def test_warm_with_no_carried_id_stamps_unresolved_not_ambient_env(monkeypatch):
    """The anti-forgery case: the ambient env holds the SERVER's id (as it
    would inside a resident warm process), and no id was carried -- the
    result must be the sentinel, never the server owner's id."""
    _ambient_env_holds(monkeypatch, _SERVER_OWNER_SID)
    with session_core.warm_served_request():
        assert _resolve_sent_by({}) == _SENT_BY_UNRESOLVED


def test_cold_with_only_env_var_set_still_stamps_it(monkeypatch):
    """The ~49-memo regression pin: cold callers still resolve from the
    ambient env when nothing else names the session -- this row does not
    reopen that gap."""
    _ambient_env_holds(monkeypatch, _CARRIED_SID)
    assert _resolve_sent_by({}) == _CARRIED_SID
