"""Pins the live-owner-first cross-repo route in the two nudge hooks."""
from __future__ import annotations

from coordinator_core.hooks import nudge_em_code_dispatch as dispatch
from coordinator_core.hooks import nudge_unauthorized_handoff as handoff


def _reason(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_foreign_checkout_deny_names_sendmessage_then_memo():
    result = dispatch._foreign_checkout_deny("/other", "/other/a.py")
    reason = _reason(result)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "`SendMessage` its live EM; none live -> `cross-repo-memo draft" in reason


def test_handoff_nudge_template_names_sendmessage_then_memo():
    assert "SendMessage the live owner; none live -> cross-repo-memo" in handoff._NUDGE_MSG_TEMPLATE
