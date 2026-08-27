"""
coordinator_core.hooks.test_suggest_sonnet_research -- tests for the
resolve_subagent_identity-routed suppression path in suggest_sonnet_research.py.

Spec backlink: coordinator_core/hooks/suggest_sonnet_research.py (module under test).
"""

from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.hooks import suggest_sonnet_research as ssr  # noqa: E402


def _run(params):
    with mock.patch.object(ssr, "_has_deep_research_plugin", return_value=False):
        return asyncio.run(ssr._handler(params))


def test_named_teammate_with_valid_session_id_suppressed():
    # Regression case: must fail before the fix and pass after.
    result = _run(
        {"agent_id": "arscout-deadbeef123456ab", "session_id": "abcdefgh-full-session"}
    )
    assert result == {}


def test_bare_hex_still_suppressed_no_regression_on_fast_path():
    result = _run({"agent_id": "abc123456789abc", "session_id": ""})
    assert result == {}


def test_garbage_agent_id_not_suppressed_advisory_fires():
    result = _run({"agent_id": "not-an-agent-id", "session_id": "abcdefgh"})
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "DELEGATION REQUIRED" in hso["additionalContext"]


def test_absent_agent_id_not_suppressed_advisory_fires():
    result = _run({"session_id": "abcdefgh"})
    hso = result["hookSpecificOutput"]
    assert "DELEGATION REQUIRED" in hso["additionalContext"]


def test_empty_agent_id_not_suppressed_advisory_fires():
    result = _run({"agent_id": "", "session_id": "abcdefgh"})
    hso = result["hookSpecificOutput"]
    assert "DELEGATION REQUIRED" in hso["additionalContext"]


def test_named_teammate_with_short_session_id_not_suppressed():
    # resolve_subagent_identity's documented fail-closed boundary: named-teammate
    # grammar requires len(session_id) >= 8; shorter than that resolves to "",
    # which is a deliberate resolver contract, not an oversight here.
    result = _run(
        {"agent_id": "arscout-deadbeef123456ab", "session_id": "short12"}
    )
    hso = result["hookSpecificOutput"]
    assert "DELEGATION REQUIRED" in hso["additionalContext"]
