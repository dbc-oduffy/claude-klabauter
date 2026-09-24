"""
coordinator_core.session.tests.test_whoami_live — JSON-RPC veneer tests
for "session.whoami_live".

Spec: docs/plans/2026-09-22-inbox-blitz-bundled-xs-s-fixes-2026-09-11.md
(item 54, T54).
"""

from __future__ import annotations

from coordinator_core.ops.session_whoami_live import _session_whoami_live
from coordinator_core.session import core, liveness


def test_live_session_shape(monkeypatch):
    monkeypatch.setattr(core, "resolve_session_id", lambda cwd=None: "sid-a")
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid == "sid-a")

    result = _session_whoami_live({})
    assert result == {"session_id": "sid-a", "live": True}


def test_dead_session_shape(monkeypatch):
    monkeypatch.setattr(core, "resolve_session_id", lambda cwd=None: "sid-a")
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: False)

    result = _session_whoami_live({})
    assert result == {"session_id": "sid-a", "live": False}


def test_unresolvable_session_id_never_calls_liveness(monkeypatch):
    """Empty `resolve_session_id()` short-circuits to `live: False` without
    calling `liveness.session_live` on an empty string."""
    monkeypatch.setattr(core, "resolve_session_id", lambda cwd=None: "")

    def _fail_if_called(sid, cwd=None):
        raise AssertionError("session_live must not be called for an empty session_id")

    monkeypatch.setattr(liveness, "session_live", _fail_if_called)

    result = _session_whoami_live({})
    assert result == {"session_id": "", "live": False}


def test_params_ignored():
    """No params are consumed -- an arbitrary dict is accepted unchanged."""
    result = _session_whoami_live({"unused": "value"})
    assert set(result.keys()) == {"session_id", "live"}
