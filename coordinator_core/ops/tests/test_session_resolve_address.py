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

import re

from coordinator_core.ops.session_resolve_address import _session_resolve_address
from coordinator_core.session import harness_registry as hr
from coordinator_core.session import messaging_gate
from coordinator_core.session import reachability


def _record(name, socket, cwd="/repo"):
    return hr.RegistryRecord(
        pid=1, start_epoch=1000.0, cwd=cwd, name=name, messaging_socket_path=socket
    )


def test_reachable_shape(monkeypatch):
    snap = {"sid-a": _record("claude-klabauter-57", "/sock/a.sock")}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)

    result = _session_resolve_address({"session_id": "sid-a"})
    assert result["outcome"] == "reachable"
    assert result["session_id"] == "sid-a"
    # Ref-qualified UNCONDITIONALLY, even for a uniquely-named sole
    # candidate: the harness refuses a bare name for a cross-session
    # SendMessage target (reachability's module docstring, measured live
    # 2026-08-13). Matched by shape rather than a literal digest so the
    # test pins the contract, not a re-typed sha256 of the fixture socket.
    assert re.fullmatch(r"claude-klabauter-57 \[[0-9a-f]{6,12}\]", result["address"])
    assert result["reason"] is None
    assert result["candidates"] == []


def test_own_session_shape(monkeypatch):
    snap = {"sid-self": _record("claude-klabauter-99", "/sock/self.sock")}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: ("sid-self", 1))

    result = _session_resolve_address({"session_id": "sid-self"})
    assert result["outcome"] == "own_session"
    assert result["session_id"] == "sid-self"
    assert result["reason"] is None
    assert result["candidates"] == []


def test_missing_param_degrades_to_not_reachable():
    result = _session_resolve_address({})
    assert result["outcome"] == "not_reachable"
    assert result["address"] is None
    assert result["reason"] == reachability.NotReachableReason.NO_OWNER_ID


def test_not_reachable_reason_reaches_the_wire(monkeypatch):
    """A live-but-unaddressable peer and a nonexistent session are both
    `not_reachable`; only `reason` tells a JSON-RPC caller which it got."""
    snap = {"sid-live": _record("claude-klabauter-57", None)}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)

    live = _session_resolve_address({"session_id": "sid-live"})
    absent = _session_resolve_address({"session_id": "sid-gone"})

    assert live["outcome"] == absent["outcome"] == "not_reachable"
    assert live["reason"] == reachability.NotReachableReason.MESSAGING_UNAVAILABLE
    assert absent["reason"] == reachability.NotReachableReason.NO_LIVE_RECORD


def test_fallback_channel_reaches_the_wire_and_is_repo_conditional(monkeypatch, tmp_path):
    """The op's own `repo_root` param drives the fallback pointer -- a
    same-tree target gets `peer_notice.send`, a cross-repo target gets the
    memo channel, threaded through as `this_repo_root` to `reachability.
    resolve_address` unchanged."""
    this_repo = tmp_path / "this-repo"
    sibling_repo = tmp_path / "sibling-repo"
    (this_repo / "subdir").mkdir(parents=True)
    sibling_repo.mkdir()

    snap = {"sid-live": _record("claude-klabauter-57", None, cwd=str(this_repo / "subdir"))}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)

    same_tree = _session_resolve_address({"session_id": "sid-live"}, repo_root=this_repo)
    assert same_tree["reason"] == reachability.NotReachableReason.MESSAGING_UNAVAILABLE
    assert same_tree["fallback_channel"] == reachability.FallbackChannel.PEER_NOTICE

    cross_repo = _session_resolve_address({"session_id": "sid-live"}, repo_root=sibling_repo)
    assert cross_repo["fallback_channel"] == reachability.FallbackChannel.CROSS_REPO_MEMO


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
        lambda owner_id, this_repo_root=None: ResolveResult(outcome="ambiguous", candidates=candidates),
    )

    result = _session_resolve_address({"session_id": "5d3d5763-aaaa"})
    assert result["outcome"] == "ambiguous"
    assert {c["session_id"] for c in result["candidates"]} == {
        "5d3d5763-aaaa",
        "5d3d5763-bbbb",
    }
    assert result["candidates"][0]["address"] == "claude-klabauter-a1 [ab]"


# ---------------------------------------------------------------------------
# caller_messaging_gate — the calling session's own gate state
# ---------------------------------------------------------------------------

def test_caller_messaging_gate_is_present_on_every_outcome(monkeypatch):
    """The gate block rides on the answer the reader already asked for.

    A separate op they have to know to call is the "the operator remembers"
    shape, not an artifact that discharges the rule.
    """
    snap = {"sid-a": _record("claude-klabauter-57", "/sock/a.sock")}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)

    reachable = _session_resolve_address({"session_id": "sid-a"})
    unmatched = _session_resolve_address({"session_id": "sid-nope"})
    no_id = _session_resolve_address({})

    for result in (reachable, unmatched, no_id):
        gate = result["caller_messaging_gate"]
        assert set(gate) == {"state", "requested", "inbox_bound", "note"}
        assert gate["state"] in {
            messaging_gate.GateState.NOT_REQUESTED,
            messaging_gate.GateState.DECLINED,
            messaging_gate.GateState.REQUESTED_UNBOUND,
            messaging_gate.GateState.OPEN,
        }


def test_caller_messaging_gate_separates_asked_and_unbound_from_never_asked(monkeypatch):
    """The whole point: `not_reachable` + `peer-messaging-unavailable` reads
    identically in both states, and only one of them is a claude-klabauter defect."""
    snap = {"sid-live": _record("claude-klabauter-57", None)}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)

    real_classify = messaging_gate.classify
    monkeypatch.setattr(
        messaging_gate,
        "classify",
        lambda environ=None: real_classify({messaging_gate.GATE_ENV_VAR: "1"}),
    )
    asked = _session_resolve_address({"session_id": "sid-live"})

    monkeypatch.setattr(
        messaging_gate, "classify", lambda environ=None: messaging_gate.MessagingGate(
            state=messaging_gate.GateState.NOT_REQUESTED,
            requested=False,
            inbox_bound=False,
            note="n",
        )
    )
    never_asked = _session_resolve_address({"session_id": "sid-live"})

    assert asked["reason"] == reachability.NotReachableReason.MESSAGING_UNAVAILABLE
    assert never_asked["reason"] == asked["reason"]
    assert asked["caller_messaging_gate"]["state"] == messaging_gate.GateState.REQUESTED_UNBOUND
    assert never_asked["caller_messaging_gate"]["state"] == messaging_gate.GateState.NOT_REQUESTED


def test_caller_messaging_gate_does_not_change_outcome_reason_or_address(monkeypatch):
    """`messaging_available()`/`resolve_address()` semantics are untouched --
    the new signal is additive and never feeds back into the resolver."""
    snap = {"sid-a": _record("claude-klabauter-57", "/sock/a.sock")}
    monkeypatch.setattr(hr, "snapshot", lambda: snap)
    monkeypatch.setattr(hr, "self_record", lambda: None)

    baseline = reachability.resolve_address("sid-a")
    result = _session_resolve_address({"session_id": "sid-a"})

    assert result["outcome"] == baseline.outcome
    assert result["address"] == baseline.address
    assert result["reason"] == baseline.reason
    assert not hasattr(baseline, "caller_messaging_gate")
