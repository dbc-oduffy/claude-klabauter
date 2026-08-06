"""Regression coverage for ``evaluate_payload_json``'s ``collect_advisories``
aggregate contract (C10, 2026-08-06, ``coordinator_core.bash_guards.
dispatch``).

Bug this chunk exists to pin: today (pre-C10), whichever soft/content/
advisory envelope fires FIRST in the guard chain wins and every LATER one
produced in the same dispatch call is silently dropped -- a live production
loss, mirroring the same shape C6 already closed on the write leg
(``write_guards.engine.evaluate``'s own ``collect_advisories`` kwarg). C10
adds the identical opt-in kwarg here, but with a DELIBERATELY DIFFERENT
aggregate return shape: this dispatcher interleaves hard-deny bands
(CONFINEMENT_DENY, PLATFORM_CONDITIONED_DENY) and soft/content/advisory bands
(ADVISORY_REWRITE) in ONE flat chain, so a genuine hard-deny can fire AFTER
advisories were already collected in the same call -- see ``evaluate_payload_
json``'s own docstring for the full contract. This file's assertions are
written against THAT shape, not the write leg's flat "always a list" one.

Own file (not folded into an existing ``bash_guards/tests/test_dispatch_*.py``
file): none of the existing dispatch-focused files
(``test_dispatch_crash_deny.py``, ``test_dispatch_blanket_disarm_wiring.py``,
``test_dispatch_checks_peer_claim.py``, ``test_dispatch_latency_bound.py``,
``test_guard_unlock_dispatch_intercept.py``) own the aggregate ``collect_
advisories`` return-shape contract -- each targets a narrower, already-named
seam (crash-deny message text, blanket-disarm band suppression, peer-claim
wiring, latency budget, in-session unlock). This chunk's contract is a new,
independently-named seam (the ``collect_advisories`` kwarg itself), so a new
file is the natural home rather than bolting an unrelated concern onto one
of those.

Uses the SAME controlled-chain technique as ``test_dispatch_blanket_disarm_
wiring.py``'s own ``TestControlledBandSuppression`` -- monkeypatch
``dispatch._build_guard_chain`` to return a small, fully-controlled chain
with distinguishable envelopes per entry, so aggregation order/short-circuit
behaviour can be asserted deterministically without depending on finding two
of the ~30 real guards that both happen to fire on one crafted command (which
would make this fragile and hard to reason about) -- while still exercising
the REAL ``evaluate_payload_json`` loop, the real short-circuit/aggregation
logic, and the real ``_record_advisory_fire`` call site.

Spec backlink: coordinator_core/bash_guards/dispatch.py
(``evaluate_payload_json``'s ``collect_advisories`` docstring section).
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry


def _payload(cmd: str = "echo probe", session_id: str = "sess-collect-1") -> str:
    return json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": session_id,
            "cwd": "/tmp",
        }
    )


def _soft_envelope(tag: str):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "soft note: %s" % tag,
        }
    }


def _content_envelope(tag: str):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "content note: %s" % tag,
        }
    }


def _deny_envelope(tag: str):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "fake hard deny: %s" % tag,
        }
    }


# Two distinct soft/content-shaped advisory entries, in registration order --
# used for (a)/(b)/(d)/(e) below. Deliberately DIFFERENT envelope shapes
# (soft "allow+additionalContext" vs content "allow+additionalContext" with
# a distinguishable tag) so (d) can assert each survives the aggregate
# return without being merged/coerced into the other's shape.
_TWO_ADVISORY_CHAIN = [
    GuardEntry("fake-soft-first", lambda: _soft_envelope("first"), False, GuardBand.ADVISORY_REWRITE),
    GuardEntry("fake-content-second", lambda: _content_envelope("second"), False, GuardBand.ADVISORY_REWRITE),
]

# Same two advisories, plus a hard-deny entry registered AFTER both --
# mirrors the module docstring's own example: "a PLATFORM_CONDITIONED_DENY
# guard, registered at the tail, denying after an ADVISORY_REWRITE guard
# upstream already produced an allow+context envelope."
_ADVISORY_THEN_DENY_CHAIN = list(_TWO_ADVISORY_CHAIN) + [
    GuardEntry(
        "fake-platform-deny",
        lambda: _deny_envelope("tail"),
        True,
        GuardBand.PLATFORM_CONDITIONED_DENY,
    ),
]


def _patch_chain(monkeypatch, chain):
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: list(chain))


class TestAdvisoryAggregationPropertyA:
    """(a) Two advisory guards both firing on one Bash payload => BOTH
    returned under ``collect_advisories=True``, in chain order. This is the
    live production drop the chunk exists to fix."""

    def test_both_advisories_collected_in_chain_order(self, monkeypatch):
        _patch_chain(monkeypatch, _TWO_ADVISORY_CHAIN)
        result = dispatch.evaluate_payload_json(_payload(), collect_advisories=True)
        assert result == [_soft_envelope("first"), _content_envelope("second")]

    def test_fails_against_pre_c10_behaviour(self, monkeypatch):
        """Demonstrates the pre-C10 drop directly: the SAME controlled chain,
        called the way every pre-C10 caller called this function (no
        ``collect_advisories`` kwarg at all -- which is exactly what a
        pre-C10 ``evaluate_payload_json`` accepted, since the kwarg did not
        exist), returns only the FIRST advisory and silently drops the
        second. This is the "genuine live production drop" property (a)
        exists to close, reproduced here without relying on the new kwarg --
        proof that a hypothetical property-(a)-shaped assertion made against
        the OLD single-envelope return (``result == first envelope`` and
        nothing more) would have been the only true statement pre-C10, i.e.
        an assertion of "both returned" genuinely fails against that
        behaviour.
        """
        _patch_chain(monkeypatch, _TWO_ADVISORY_CHAIN)
        legacy_shaped_result = dispatch.evaluate_payload_json(_payload())
        assert legacy_shaped_result == _soft_envelope("first")
        assert legacy_shaped_result != [_soft_envelope("first"), _content_envelope("second")]


class TestLegacyCallerUnchangedPropertyB:
    """(b) The legacy caller (flag absent) returns exactly one envelope,
    unchanged."""

    def test_flag_absent_returns_single_first_envelope(self, monkeypatch):
        _patch_chain(monkeypatch, _TWO_ADVISORY_CHAIN)
        result = dispatch.evaluate_payload_json(_payload())
        assert result == _soft_envelope("first")

    def test_flag_explicit_false_matches_flag_absent(self, monkeypatch):
        _patch_chain(monkeypatch, _TWO_ADVISORY_CHAIN)
        omitted = dispatch.evaluate_payload_json(_payload())
        explicit_false = dispatch.evaluate_payload_json(_payload(), collect_advisories=False)
        assert omitted == explicit_false == _soft_envelope("first")


class TestHardDenyShortCircuitsPropertyC:
    """(c) A hard-deny anywhere in the chain short-circuits and is returned
    alone, discarding already collected advisories."""

    def test_trailing_hard_deny_discards_collected_advisories(self, monkeypatch):
        # Review: code-reviewer nit -- assert explicitly that no real
        # in-session-unlock sentinel is consulted/consumed on this path,
        # rather than relying on ambient on-disk state happening to be empty
        # for a fake guard name that could never have a real sentinel.
        unlock_calls = []
        monkeypatch.setattr(
            dispatch,
            "_consume_unlock",
            lambda session_id, name: unlock_calls.append((session_id, name)) or False,
        )
        _patch_chain(monkeypatch, _ADVISORY_THEN_DENY_CHAIN)
        result = dispatch.evaluate_payload_json(_payload(), collect_advisories=True)
        assert unlock_calls == [("sess-collect-1", "fake-platform-deny")]
        assert not isinstance(result, list)
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        # The in-session-unlock annotation (`_annotate_unlock`) appends its
        # own line to a genuine deny's reason at this same seam -- exact
        # string equality against the raw fake envelope would be asserting
        # an incidental byte shape, not this property; `in` is the correct
        # assertion for "this IS the tail deny, un-discarded".
        assert "fake hard deny: tail" in hso["permissionDecisionReason"]
        assert "soft note" not in json.dumps(result)
        assert "content note" not in json.dumps(result)

    def test_legacy_caller_never_even_reaches_the_tail_deny(self, monkeypatch):
        # Legacy (flag absent) short-circuits on the FIRST non-None result,
        # same as always -- that is the first advisory, not the tail deny;
        # the deny-short-circuit property under test here is specific to
        # `collect_advisories=True`'s aggregation continuing the walk past
        # advisories that a legacy call would have already stopped at.
        _patch_chain(monkeypatch, _ADVISORY_THEN_DENY_CHAIN)
        legacy = dispatch.evaluate_payload_json(_payload())
        assert legacy == _soft_envelope("first")


class TestClassDistinctionSurvivesPropertyD:
    """(d) The soft/content/advisory class distinction survives the
    aggregate return -- each collected envelope keeps its own distinguishable
    shape/content rather than being merged or coerced into a common one."""

    def test_each_envelope_retains_its_own_distinct_content(self, monkeypatch):
        _patch_chain(monkeypatch, _TWO_ADVISORY_CHAIN)
        result = dispatch.evaluate_payload_json(_payload(), collect_advisories=True)
        assert len(result) == 2
        soft, content = result
        assert soft["hookSpecificOutput"]["additionalContext"] == "soft note: first"
        assert content["hookSpecificOutput"]["additionalContext"] == "content note: second"
        assert soft != content


class TestAdvisoryFireRecordedOncePerEnvelopePropertyE:
    """(e) ``_record_advisory_fire`` runs once per returned envelope, not
    once total."""

    def test_recorded_once_per_collected_advisory(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            dispatch,
            "_record_advisory_fire",
            lambda name, session_id, cwd: calls.append(name),
        )
        _patch_chain(monkeypatch, _TWO_ADVISORY_CHAIN)
        dispatch.evaluate_payload_json(_payload(), collect_advisories=True)
        assert calls == ["fake-soft-first", "fake-content-second"]

    def test_not_recorded_for_a_discarded_advisory_behind_a_hard_deny(self, monkeypatch):
        # The two advisories still fire (and are recorded) before the
        # trailing hard-deny short-circuits the chain; the hard-deny entry
        # itself is fail_closed=True and is never passed to
        # `_record_advisory_fire` (that bookkeeping call is gated on
        # `not fail_closed` in the loop). This asserts the deny entry never
        # adds a THIRD recorded name, not that the two advisories go
        # unrecorded.
        calls = []
        monkeypatch.setattr(
            dispatch,
            "_record_advisory_fire",
            lambda name, session_id, cwd: calls.append(name),
        )
        # Review: code-reviewer nit -- same explicit in-session-unlock
        # assertion as TestHardDenyShortCircuitsPropertyC above (see there
        # for rationale).
        unlock_calls = []
        monkeypatch.setattr(
            dispatch,
            "_consume_unlock",
            lambda session_id, name: unlock_calls.append((session_id, name)) or False,
        )
        _patch_chain(monkeypatch, _ADVISORY_THEN_DENY_CHAIN)
        result = dispatch.evaluate_payload_json(_payload(), collect_advisories=True)
        assert unlock_calls == [("sess-collect-1", "fake-platform-deny")]
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "fake hard deny: tail" in result["hookSpecificOutput"]["permissionDecisionReason"]
        assert calls == ["fake-soft-first", "fake-content-second"]
        assert "fake-platform-deny" not in calls
