"""
coordinator_core.pickup_assemble.tests.test_sender_reachability

Purpose: pins C8's `compute_sender_reachability` render — the memo-pickup
surface that resolves C7's `sent_by` (docs/plans/2026-08-13-session-
identity-earns-its-keep.md, chunk C8) through
`coordinator_core.session.reachability.resolve_address` and renders each of
its four `ResolveResult.outcome` arms plus C7's `_SENT_BY_UNRESOLVED`
sentinel arm, which is NOT one of the four.

Spec backlink: docs/plans/2026-08-13-session-identity-earns-its-keep.md §
AC8 (four-arm render, `not_reachable` pinned as the ordinary/dominant case,
never the warning register) and § AC9 (no address is ever replayed — every
render is resolved at call time, never trusted off a persisted decision
object).

Negative-spec: this file does NOT touch `compute_competing_claim`/
`compute_successor_handoffs` or their `send_message_address` render —
those resolve CLAIM HOLDERS for a contested artifact and are already
covered by `test_claim_state_reads.py`; this file covers only the memo-
sender-reachability render, which is a different wording over the same
`reachability` mechanism (C8 dispatch brief: "do NOT reuse the
competing-claim wording").

Run (from the repo root): python -m pytest
coordinator_core/pickup_assemble/tests/test_sender_reachability.py -q
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import coordinator_core.pickup_assemble as pa
from coordinator_core.session import reachability as reachability_mod


@dataclass
class _FakeResolveResult:
    outcome: str
    session_id: Optional[str] = None
    address: Optional[str] = None
    candidates: list = field(default_factory=list)


_WARNING_REGISTER = ("warning", "unavailable", "could not", "failed", "unfortunately", "⚠", "!")


def test_empty_sent_by_renders_nothing():
    assert pa.compute_sender_reachability(None) == {}
    assert pa.compute_sender_reachability("") == {}


def test_sentinel_unresolved_arm_is_not_one_of_the_four(monkeypatch):
    # A resolve_address call would be a bug for this arm — an "unresolved"
    # string is not a session id. Fail loud if it's ever called.
    def _boom(_owner_id):
        raise AssertionError("resolve_address must not be called for the C7 sentinel")

    monkeypatch.setattr(reachability_mod, "resolve_address", _boom)

    result = pa.compute_sender_reachability("unresolved")

    assert result["outcome"] == "sender_unresolved"
    assert result["message"] == (
        "This memo's sender identity was never resolved at send time "
        "— no reachability to compute."
    )
    assert result["address"] == ""
    assert result["resolved_at"]


def test_own_session_arm_names_self_receipt(monkeypatch):
    monkeypatch.setattr(
        reachability_mod, "resolve_address", lambda sid: _FakeResolveResult(outcome="own_session", session_id=sid)
    )

    result = pa.compute_sender_reachability("11111111-1111-1111-1111-111111111111")

    assert result["outcome"] == "own_session"
    assert result["message"] == (
        "This memo was sent by this same session — a self-receipt, not a reply target."
    )
    assert result["resolved_at"]


def test_reachable_arm_renders_the_address(monkeypatch):
    monkeypatch.setattr(
        reachability_mod,
        "resolve_address",
        lambda sid: _FakeResolveResult(outcome="reachable", session_id=sid, address="abc123abc123"),
    )
    monkeypatch.setattr(reachability_mod, "resolve_advisory_address", lambda sid: "abc123abc123")

    result = pa.compute_sender_reachability("22222222-2222-2222-2222-222222222222")

    assert result["outcome"] == "reachable"
    assert result["message"] == "Sender is reachable — reply via SendMessage to abc123abc123."
    assert result["address"] == "abc123abc123"
    assert result["resolved_at"]


def test_reachable_arm_with_falsy_address_degrades_to_not_reachable(monkeypatch):
    """Guards a future `resolve_address` contract change, not an observed
    live case: today no input can produce a "reachable" outcome with a
    falsy or "<this session>" address (see the comment at the call site),
    but the degrade branch exists in case that contract ever changes.
    Exercise it directly so it stays proven correct rather than merely
    plausible.
    """
    monkeypatch.setattr(
        reachability_mod,
        "resolve_address",
        lambda sid: _FakeResolveResult(outcome="reachable", session_id=sid, address=""),
    )

    result = pa.compute_sender_reachability("33333333-3333-3333-3333-333333333333")

    assert result["outcome"] == "not_reachable"
    assert result["message"] == "Sender's session has ended — action this the normal way."
    assert result["address"] == ""


def test_reachable_arm_with_placeholder_address_degrades_to_not_reachable(monkeypatch):
    """Same degrade, hit via the other falsy-equivalent value: an address
    literally equal to "<this session>" (reserved for the own_session arm)
    must not be rendered as a reply target either.
    """
    monkeypatch.setattr(
        reachability_mod,
        "resolve_address",
        lambda sid: _FakeResolveResult(outcome="reachable", session_id=sid, address="<this session>"),
    )

    result = pa.compute_sender_reachability("44444444-4444-4444-4444-444444444444")

    assert result["outcome"] == "not_reachable"
    assert result["message"] == "Sender's session has ended — action this the normal way."
    assert result["address"] == ""


def test_reachable_arm_does_not_take_a_second_live_registry_scan(monkeypatch):
    """`result.address` from the one `resolve_address` call already carries
    the answer -- `resolve_advisory_address` re-running a second, independent
    `harness_registry.snapshot()` scan for the reachable arm is the exact
    one-snapshot-discipline regression (68f8c14ce) the review that added this
    test found live. Fail loud if it's ever called again.
    """
    monkeypatch.setattr(
        reachability_mod,
        "resolve_address",
        lambda sid: _FakeResolveResult(outcome="reachable", session_id=sid, address="cafefeed0000"),
    )

    def _boom(_sid):
        raise AssertionError("resolve_advisory_address must not be called for the reachable arm")

    monkeypatch.setattr(reachability_mod, "resolve_advisory_address", _boom)

    result = pa.compute_sender_reachability("77777777-7777-7777-7777-777777777777")

    assert result["outcome"] == "reachable"
    assert result["address"] == "cafefeed0000"
    assert result["message"] == "Sender is reachable — reply via SendMessage to cafefeed0000."


def test_not_reachable_arm_is_the_dominant_ordinary_render(monkeypatch):
    monkeypatch.setattr(reachability_mod, "resolve_address", lambda sid: _FakeResolveResult(outcome="not_reachable"))

    result = pa.compute_sender_reachability("33333333-3333-3333-3333-333333333333")

    assert result["outcome"] == "not_reachable"
    assert result["message"] == "Sender's session has ended — action this the normal way."
    assert result["address"] == ""
    assert result["resolved_at"]

    # AC8 forcing function: prose asserting "ordinary" tone is not enough —
    # the render must contain none of the warning register.
    lowered = result["message"].lower()
    for banned in _WARNING_REGISTER:
        assert banned not in lowered, f"not_reachable render leaked warning-register token: {banned!r}"


def test_ambiguous_arm_does_not_collapse_into_not_reachable(monkeypatch):
    monkeypatch.setattr(
        reachability_mod,
        "resolve_address",
        lambda sid: _FakeResolveResult(outcome="ambiguous", candidates=[object(), object()]),
    )

    result = pa.compute_sender_reachability("44444444-4444-4444-4444-444444444444")

    assert result["outcome"] == "ambiguous"
    assert result["outcome"] != "not_reachable"
    assert result["message"] == (
        "Sender's session id matches more than one live session — "
        "reachability is ambiguous, not confirmed."
    )
    assert result["resolved_at"]


def test_resolver_import_or_call_failure_degrades_to_not_reachable(monkeypatch):
    def _boom(_sid):
        raise RuntimeError("harness_registry.snapshot() blew up")

    monkeypatch.setattr(reachability_mod, "resolve_address", _boom)

    result = pa.compute_sender_reachability("55555555-5555-5555-5555-555555555555")

    assert result["outcome"] == "not_reachable"
    assert result["message"] == "Sender's session has ended — action this the normal way."


def test_render_is_resolved_at_call_time_not_trusted_off_a_persisted_decision_object(monkeypatch):
    """AC9: a re-read decision object is not trusted for contact.

    Simulates the real lifecycle -- brief() resolves `reachable` once, that
    dict gets persisted verbatim into a decision-object json (per
    68f8c14ce's sanctioned exception), and the sender's session then ends
    before the NEXT brief re-resolves the same `sent_by`. The second call
    must reflect the live (now not_reachable) state, never the stale
    persisted address -- proving this function always re-resolves rather
    than trusting anything cached from a prior call.
    """
    sent_by = "66666666-6666-6666-6666-666666666666"

    monkeypatch.setattr(
        reachability_mod,
        "resolve_address",
        lambda sid: _FakeResolveResult(outcome="reachable", session_id=sid, address="deadbeef0000"),
    )
    monkeypatch.setattr(reachability_mod, "resolve_advisory_address", lambda sid: "deadbeef0000")

    first = pa.compute_sender_reachability(sent_by)
    assert first["outcome"] == "reachable"
    assert first["address"] == "deadbeef0000"

    # "Persist" it verbatim, as the decision-object write path does.
    persisted_decision_object = {"gates": {"sender_reachability": dict(first)}}

    # The sender's session has since ended -- the live registry no longer
    # matches. This function must not read `persisted_decision_object` at
    # all; it only ever takes `sent_by` (the durable UUID).
    monkeypatch.setattr(reachability_mod, "resolve_address", lambda sid: _FakeResolveResult(outcome="not_reachable"))

    second = pa.compute_sender_reachability(sent_by)

    assert second["outcome"] == "not_reachable"
    assert second["address"] == ""
    # The stale persisted copy is untouched -- proving nothing routed
    # through it -- and disagrees with the fresh render, which is the point.
    assert persisted_decision_object["gates"]["sender_reachability"]["address"] == "deadbeef0000"
    assert second["address"] != persisted_decision_object["gates"]["sender_reachability"]["address"]


def test_sentinel_literal_matches_the_historical_send_path_spelling():
    """Pin `pickup_assemble._SENT_BY_UNRESOLVED` to the literal every already-
    delivered memo on disk was written with.

    `ops.fleet.memo_send` (the writer that used to own this sentinel) and its
    CLI counterpart (`coordinator/bin/cross-repo-memo.py`'s own copy) were
    both removed 2026-08-23 when memo.send was killed (PM ruling: a killed op
    dies outright, no stub) — there is no live writer of `sent_by:` left to
    pin this reader against. `pickup_assemble` still needs its own copy: it
    reads `sent_by:` off memos already delivered under the old op, and a
    changed literal here would silently stop recognising them (every
    unresolved-sender memo would fall through to `resolve_address` with a
    non-uuid string, rendering `not_reachable` — a wrong fact, and a quiet
    one). Pinned to the literal itself now, not a sibling module's copy.
    """
    assert pa._SENT_BY_UNRESOLVED == "unresolved", (
        "pickup_assemble._SENT_BY_UNRESOLVED must keep reading the literal "
        "every already-delivered memo's sent_by: field was written with "
        "(the writer that used it, memo.send, was killed 2026-08-23 — this "
        "reader still has to make sense of the memos it left behind)."
    )


def test_resolver_produces_no_arm_this_render_does_not_handle():
    """A fifth `ResolveResult.outcome` must break this test, not degrade silently.

    `ResolveResult.outcome` is a bare `str`, not an enum or a `Literal` — so
    nothing structurally constrains the arm set, and `compute_sender_reachability`
    ends in an `else` that relabels any unrecognised outcome as `not_reachable`
    and renders "Sender's session has ended". For the four arms that exist today
    that fallback is unreachable. If a fifth is ever added upstream, it becomes a
    confident false statement about a sender who may be sitting right there —
    the same collapse the plan's anti-scope forbids for `ambiguous`, arriving by
    a different door and without a test failing.

    So pin the producer's arm set against the consumer's. This asserts nothing
    about the fallback's correctness; it asserts that adding an arm cannot be
    silent.
    """
    import re
    from pathlib import Path

    from coordinator_core.session import reachability as reachability_mod

    src = Path(reachability_mod.__file__).read_text(encoding="utf-8")
    produced = set(re.findall(r'ResolveResult\(\s*outcome=["\']([a-z_]+)["\']', src))

    handled = {"own_session", "reachable", "not_reachable", "ambiguous"}

    assert produced == handled, (
        "coordinator_core.session.reachability produces outcome arm(s) that "
        "pickup_assemble.compute_sender_reachability does not explicitly render: "
        f"{sorted(produced - handled)}. Add an explicit render arm there (and a "
        "test here) rather than letting the trailing `else` relabel it as "
        "not_reachable — see this test's docstring."
    )
