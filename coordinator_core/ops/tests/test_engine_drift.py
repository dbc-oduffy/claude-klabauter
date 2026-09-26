
from __future__ import annotations

import subprocess
from unittest.mock import patch

from coordinator_core.ops.engine_drift import (
    _engine_drift,
    _git_is_behind,
    classify_drift,
)
from coordinator_core.win_portability import no_console_creationflags

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

FLOOR = "6fdc7b4de770dc1c996b3c2a42bf2c7984dd67c9"
RUNNING = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


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
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert result["state"] != "clean"
        assert "notice" in result

    def test_unknown_sha_not_behind_alarm(self):
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


# history rather than a throwaway repo fixture, since MIN_KNOWN_GOOD_SHA is a known
class TestGitIsBehindEqualShaShortCircuit:
    def test_equal_sha_returns_false_without_subprocess(self):
        with patch("coordinator_core.ops.engine_drift.git_predicate") as mock_probe:
            result = _git_is_behind(FLOOR, FLOOR)
        assert result is False
        mock_probe.assert_not_called()


class TestGitIsBehindRealAncestry:
    def test_ancestor_sha_returns_true(self):
        # MIN_KNOWN_GOOD_SHA is an ancestor of this checkout's HEAD by construction —
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd="coordinator_core",
            **no_console_creationflags(),
        ).stdout.strip()
        result = _git_is_behind(FLOOR, head)
        assert result is True

    def test_descendant_sha_returns_false(self):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd="coordinator_core",
            **no_console_creationflags(),
        ).stdout.strip()
        result = _git_is_behind(head, FLOOR)
        assert result is False

    def test_unresolvable_sha_returns_none(self):
        result = _git_is_behind(
            "ffffffffffffffffffffffffffffffffffffff", FLOOR
        )
        assert result is None


# some other pair), passes MIN_KNOWN_GOOD_SHA as the floor, or that register_op("engine.drift")
class TestEngineDriftHandlerWiring:
    def test_smoke_does_not_raise_and_returns_state(self):
        result = _engine_drift({})
        assert result["state"] in ("clean", "behind", "indeterminate")

    def test_wiring_calls_resolve_engine_sha_and_git_is_behind_with_floor_constant(self):
        with patch(
            "coordinator_core.ops.engine_drift.resolve_engine_sha",
            return_value=RUNNING,
        ) as mock_resolve, patch(
            "coordinator_core.ops.engine_drift._git_is_behind",
            return_value=False,
        ) as mock_is_behind:
            result = _engine_drift({})

        mock_resolve.assert_called_once_with()
        # Argument order + floor constant: _git_is_behind(running_sha, MIN_KNOWN_GOOD_SHA),
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
            result = _engine_drift({})

        assert result["state"] == "indeterminate"
        assert result["running_sha"] is None
        mock_is_behind.assert_not_called()
