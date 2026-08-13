"""
coordinator_core.ops.tests.test_session_resolve_address — JSON-RPC veneer
tests for "session.resolve_address".

Spec backlink: `state/handoffs/2026-08-13-session-owner-reachability-registry.md` § 1

Monkeypatch-target note: these tests patch `hr.snapshot`/`hr.self_record` at
the `harness_registry` module level (`monkeypatch.setattr(hr, ...)`). That is
valid ONLY because `reachability.py` calls them via qualified attribute
access (`harness_registry.snapshot()`), never a `from ... import snapshot`.
If `reachability.py`'s import style ever changes to a bare `from` import,
these patches silently stop taking effect and the tests would start
exercising the real (unpatched) registry.
"""

from __future__ import annotations

from coordinator_core.ops.session_resolve_address import _session_resolve_address
from coordinator_core.session import harness_registry as hr


def _record(name, socket):
    return hr.RegistryRecord(
        pid=1, start_epoch=1000.0, cwd="/repo", name=name, messaging_socket_path=socket
    )


def test_reachable_shape(monkeypatch):
    snap = {"sid-a": _record("claude-klabauter-57", "/sock/a.sock")}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)

    result = _session_resolve_address({"session_id": "sid-a"})
    assert result["outcome"] == "reachable"
    assert result["session_id"] == "sid-a"
    assert result["address"] == "claude-klabauter-57"
    assert result["candidates"] == []


def test_own_session_shape(monkeypatch):
    snap = {"sid-self": _record("claude-klabauter-99", "/sock/self.sock")}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: ("sid-self", 1))

    result = _session_resolve_address({"session_id": "sid-self"})
    assert result["outcome"] == "own_session"
    assert result["session_id"] == "sid-self"
    assert result["candidates"] == []


def test_missing_param_degrades_to_not_reachable():
    result = _session_resolve_address({})
    assert result["outcome"] == "not_reachable"
    assert result["address"] is None


def test_ambiguous_shape(monkeypatch):
    # `ambiguous` cannot be produced through the live seam any more
    # (harness_registry.snapshot() de-duplicates by sessionId at parse
    # time -- see reachability.resolve_address's own docstring), so this
    # test monkeypatches resolve_address itself and asserts the op is a
    # faithful pass-through of an `ambiguous` ResolveResult's shape.
    from coordinator_core.ops import session_resolve_address as mod
    from coordinator_core.session.reachability import Candidate, ResolveResult

    candidates = [
        Candidate(session_id="5d3d5763-aaaa", name="claude-klabauter-a1", ref="ab", address="claude-klabauter-a1 [ab]"),
        Candidate(session_id="5d3d5763-bbbb", name="claude-klabauter-b2", ref="cd", address="claude-klabauter-b2 [cd]"),
    ]
    monkeypatch.setattr(
        mod.reachability,
        "resolve_address",
        lambda owner_id: ResolveResult(outcome="ambiguous", candidates=candidates),
    )

    result = _session_resolve_address({"session_id": "5d3d5763-aaaa"})
    assert result["outcome"] == "ambiguous"
    assert {c["session_id"] for c in result["candidates"]} == {
        "5d3d5763-aaaa",
        "5d3d5763-bbbb",
    }
    assert result["candidates"][0]["address"] == "claude-klabauter-a1 [ab]"
