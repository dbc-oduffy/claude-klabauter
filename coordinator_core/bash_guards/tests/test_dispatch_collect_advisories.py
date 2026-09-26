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

from coordinator_core.bash_guards import _verdict, dispatch
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


# used for (a)/(b)/(d)/(e) below. Deliberately DIFFERENT envelope shapes
_TWO_ADVISORY_CHAIN = [
    GuardEntry("fake-soft-first", lambda: _soft_envelope("first"), False, GuardBand.ADVISORY_REWRITE),
    GuardEntry("fake-content-second", lambda: _content_envelope("second"), False, GuardBand.ADVISORY_REWRITE),
]

# mirrors the module docstring's own example: "a PLATFORM_CONDITIONED_DENY
# guard, registered at the tail, denying after an ADVISORY_REWRITE guard
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


def _silent_then_allow(guard_name: str, reason: str):
    _verdict.record_silent(guard_name, reason)
    return None


_SILENT_RECORDING_CHAIN = [
    GuardEntry(
        "fake-silent-guard",
        lambda: _silent_then_allow("fake-silent-guard", "cannot parse PowerShell backtick continuation"),
        False,
        GuardBand.ADVISORY_REWRITE,
    ),
]

_SILENT_THEN_ADVISORY_CHAIN = list(_SILENT_RECORDING_CHAIN) + [
    GuardEntry("fake-soft-first", lambda: _soft_envelope("first"), False, GuardBand.ADVISORY_REWRITE),
]


class TestAdvisoryAggregationPropertyA:

    def test_both_advisories_collected_in_chain_order(self, monkeypatch):
        _patch_chain(monkeypatch, _TWO_ADVISORY_CHAIN)
        result = dispatch.evaluate_payload_json(_payload(), collect_advisories=True)
        assert result == [_soft_envelope("first"), _content_envelope("second")]

    def test_fails_against_pre_c10_behaviour(self, monkeypatch):
        _patch_chain(monkeypatch, _TWO_ADVISORY_CHAIN)
        legacy_shaped_result = dispatch.evaluate_payload_json(_payload())
        assert legacy_shaped_result == _soft_envelope("first")
        assert legacy_shaped_result != [_soft_envelope("first"), _content_envelope("second")]


class TestLegacyCallerUnchangedPropertyB:

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

    def test_trailing_hard_deny_discards_collected_advisories(self, monkeypatch):
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
        assert "fake hard deny: tail" in hso["permissionDecisionReason"]
        assert "soft note" not in json.dumps(result)
        assert "content note" not in json.dumps(result)

    def test_legacy_caller_never_even_reaches_the_tail_deny(self, monkeypatch):
        _patch_chain(monkeypatch, _ADVISORY_THEN_DENY_CHAIN)
        legacy = dispatch.evaluate_payload_json(_payload())
        assert legacy == _soft_envelope("first")


class TestClassDistinctionSurvivesPropertyD:

    def test_each_envelope_retains_its_own_distinct_content(self, monkeypatch):
        _patch_chain(monkeypatch, _TWO_ADVISORY_CHAIN)
        result = dispatch.evaluate_payload_json(_payload(), collect_advisories=True)
        assert len(result) == 2
        soft, content = result
        assert soft["hookSpecificOutput"]["additionalContext"] == "soft note: first"
        assert content["hookSpecificOutput"]["additionalContext"] == "content note: second"
        assert soft != content


class TestAdvisoryFireRecordedOncePerEnvelopePropertyE:

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
        calls = []
        monkeypatch.setattr(
            dispatch,
            "_record_advisory_fire",
            lambda name, session_id, cwd: calls.append(name),
        )
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


class TestSilentNeverReachesDispatchReturnValue:

    def test_legacy_path_silent_recording_guard_allows_silently(self, monkeypatch):
        _patch_chain(monkeypatch, _SILENT_RECORDING_CHAIN)
        result = dispatch.evaluate_payload_json(_payload())
        assert result is None

    def test_collect_advisories_silent_recording_guard_contributes_nothing(self, monkeypatch):
        _patch_chain(monkeypatch, _SILENT_RECORDING_CHAIN)
        result = dispatch.evaluate_payload_json(_payload(), collect_advisories=True)
        assert result is None

    def test_silent_recording_alongside_a_real_advisory_does_not_leak_into_the_aggregate(self, monkeypatch):
        _patch_chain(monkeypatch, _SILENT_THEN_ADVISORY_CHAIN)
        result = dispatch.evaluate_payload_json(_payload(), collect_advisories=True)
        assert result == [_soft_envelope("first")]
        assert "SILENT" not in json.dumps(result)

    def test_declaration_is_observable_only_to_a_caller_that_opens_its_own_collection(self):
        with _verdict.collecting() as silences:
            _silent_then_allow("fake-silent-guard", "cannot parse PowerShell backtick continuation")
        assert _verdict.was_silent("fake-silent-guard", silences)
