"""
coordinator_core.ops.tests.test_engine_drift

Tests for the pure classify_drift() decision core of the "engine.drift" op, plus the
git-I/O boundary (_git_is_behind) and the registered-op wiring (_engine_drift).

Drives classify_drift() with a stub is_behind callable so all three states are
deterministic and independent of the actual git ancestry of this checkout (see
engine_drift.py's module docstring for why git I/O is kept out of the pure core).

Coverage:
  (a) behind-floor  — running resolved + is_behind -> True  => state "behind", offer present.
  (b) at-or-ahead   — running resolved + is_behind -> False => state "clean", silent
                       (no "offer" key, no "notice" key).
  (c) unknown-sha   — running_sha=None => state "indeterminate"; the distinct indeterminate
                       notice is present and is neither silence nor a behind-floor alarm
                       (no "offer" key; distinct "notice" key; is_behind never called).
  (d) indeterminate-ancestry — running resolved but is_behind -> None => state
                       "indeterminate" via the second branch (ancestry indeterminable,
                       not sentinel-unresolved).
  (e) _git_is_behind — real ancestry check against this repo's own git history
                       (Review: code-reviewer Finding 1 — rc0/rc1/other-rc mapping
                       was previously unexercised by any test).
  (f) _engine_drift  — registered-op wiring smoke test + argument-order/floor-constant
                       verification via monkeypatch (Review: code-reviewer Finding 2).
"""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import patch

from coordinator_core.ops.engine_drift import (
    _engine_drift,
    _git_is_behind,
    classify_drift,
)

FLOOR = "6fdc7b4de770dc1c996b3c2a42bf2c7984dd67c9"
RUNNING = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency.

    Convention matches ops/emit/tests/test_c4a_handler_wiring.py and
    ops/fleet/tests/test_memo_send.py.
    """
    return asyncio.run(coro)


class TestClassifyDriftBehind:
    def test_behind_floor_state(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: True)
        assert result["state"] == "behind"

    def test_behind_floor_offer_present(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: True)
        assert "offer" in result
        assert result["offer"]
        assert RUNNING[:12] in result["offer"]
        assert FLOOR[:12] in result["offer"]

    def test_behind_floor_no_notice_key(self):
        # "notice" is the indeterminate-state key; behind must not carry it.
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: True)
        assert "notice" not in result

    def test_behind_floor_carries_shas(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: True)
        assert result["running_sha"] == RUNNING
        assert result["floor_sha"] == FLOOR


class TestClassifyDriftClean:
    def test_at_or_ahead_state(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: False)
        assert result["state"] == "clean"

    def test_at_or_ahead_is_silent(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: False)
        assert "offer" not in result
        assert "notice" not in result

    def test_at_or_ahead_carries_shas(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: False)
        assert result["running_sha"] == RUNNING
        assert result["floor_sha"] == FLOOR


class TestClassifyDriftIndeterminateUnresolvedSha:
    def test_unknown_sha_state(self):
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert result["state"] == "indeterminate"

    def test_unknown_sha_distinct_notice_present(self):
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert "notice" in result
        assert result["notice"]
        assert "indeterminate" in result["notice"].lower()

    def test_unknown_sha_not_silent(self):
        # Must NOT be the clean/silent shape — a notice must be present.
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert result["state"] != "clean"
        assert "notice" in result

    def test_unknown_sha_not_behind_alarm(self):
        # Must NOT be classified as "behind" even though the stub is_behind would
        # say True — an unresolved sentinel must short-circuit before is_behind
        # is ever consulted (asserted directly below).
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert result["state"] != "behind"
        assert "offer" not in result

    def test_unknown_sha_is_behind_never_called(self):
        calls = []

        def _tracking_is_behind(r, f):
            calls.append((r, f))
            return True

        classify_drift(None, FLOOR, is_behind=_tracking_is_behind)
        assert calls == []

    def test_unknown_sha_running_sha_field_is_none(self):
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert result["running_sha"] is None
        assert result["floor_sha"] == FLOOR


class TestClassifyDriftIndeterminateAncestry:
    def test_indeterminate_ancestry_state(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: None)
        assert result["state"] == "indeterminate"

    def test_indeterminate_ancestry_distinct_notice(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: None)
        assert "notice" in result
        assert "offer" not in result

    def test_indeterminate_ancestry_carries_running_sha(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: None)
        assert result["running_sha"] == RUNNING
        assert result["floor_sha"] == FLOOR


# Review: code-reviewer (Finding 1) — _git_is_behind's merge-base --is-ancestor rc
# mapping was entirely unexercised; a regression flipping the rc0/rc1 branches would
# have passed the full suite silently. Exercised against this repo's own real git
# history rather than a throwaway repo fixture, since MIN_KNOWN_GOOD_SHA is a known
# ancestor of HEAD in this checkout by construction (the floor is always <= HEAD).
class TestGitIsBehindEqualShaShortCircuit:
    def test_equal_sha_returns_false_without_subprocess(self):
        # The git seam is `git_scope.git_predicate` (which owns the 0/1/other
        # tri-state AND the stripped repo-scoping environment `-C` needs), not a
        # bare `subprocess.run` — patching the seam keeps this asserting what it
        # always asserted: the equal-sha short circuit spawns nothing at all.
        with patch("coordinator_core.ops.engine_drift.git_predicate") as mock_probe:
            result = _git_is_behind(FLOOR, FLOOR)
        assert result is False
        mock_probe.assert_not_called()


class TestGitIsBehindRealAncestry:
    def test_ancestor_sha_returns_true(self):
        # MIN_KNOWN_GOOD_SHA is an ancestor of this checkout's HEAD by construction —
        # the floor is a committed-in-the-past SHA relative to any checkout built on it.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd="coordinator_core"
        ).stdout.strip()
        result = _git_is_behind(FLOOR, head)
        assert result is True

    def test_descendant_sha_returns_false(self):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd="coordinator_core"
        ).stdout.strip()
        result = _git_is_behind(head, FLOOR)
        assert result is False

    def test_unresolvable_sha_returns_none(self):
        # A syntactically SHA-shaped but non-existent object — merge-base errors
        # (rc != 0, != 1), landing the "indeterminate" branch, never a false True/False.
        result = _git_is_behind(
            "ffffffffffffffffffffffffffffffffffffff", FLOOR
        )
        assert result is None


# Review: code-reviewer (Finding 2) — the registered-op wiring (_engine_drift) had zero
# test coverage: no test verified it calls resolve_engine_sha() + _git_is_behind() (not
# some other pair), passes MIN_KNOWN_GOOD_SHA as the floor, or that register_op("engine.drift")
# application doesn't blow up on import.
class TestEngineDriftHandlerWiring:
    def test_smoke_does_not_raise_and_returns_state(self):
        # No monkeypatching — runs against this checkout's real git state. Whatever
        # the actual state resolves to, the handler must not raise and must return
        # one of the three-value enum.
        result = _run(_engine_drift({}))
        assert result["state"] in ("clean", "behind", "indeterminate")

    def test_wiring_calls_resolve_engine_sha_and_git_is_behind_with_floor_constant(self):
        with patch(
            "coordinator_core.ops.engine_drift.resolve_engine_sha",
            return_value=RUNNING,
        ) as mock_resolve, patch(
            "coordinator_core.ops.engine_drift._git_is_behind",
            return_value=False,
        ) as mock_is_behind:
            result = _run(_engine_drift({}))

        mock_resolve.assert_called_once_with()
        # Argument order + floor constant: _git_is_behind(running_sha, MIN_KNOWN_GOOD_SHA),
        # not swapped and not a hardcoded/stale value.
        mock_is_behind.assert_called_once_with(RUNNING, FLOOR)
        assert result["state"] == "clean"
        assert result["running_sha"] == RUNNING
        assert result["floor_sha"] == FLOOR

    def test_wiring_delegates_unresolved_sha_to_classify_drift(self):
        with patch(
            "coordinator_core.ops.engine_drift.resolve_engine_sha",
            return_value=None,
        ), patch(
            "coordinator_core.ops.engine_drift._git_is_behind",
        ) as mock_is_behind:
            result = _run(_engine_drift({}))

        assert result["state"] == "indeterminate"
        assert result["running_sha"] is None
        # is_behind must never be consulted when running_sha is unresolved —
        # classify_drift's short-circuit, verified end-to-end through the handler.
        mock_is_behind.assert_not_called()
