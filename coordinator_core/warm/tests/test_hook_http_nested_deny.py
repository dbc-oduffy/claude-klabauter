"""A handler's deny nested in `hookSpecificOutput` must survive the HTTP transport."""

import json

from coordinator_core.warm.hook_http import interpret_result


def _frame(result):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode("utf-8")


def test_nested_deny_reaches_the_harness():
    body = interpret_result(
        "PreToolUse",
        _frame(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "subagent may not run the suite",
                }
            }
        ),
    )
    hso = body["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == "subagent may not run the suite"


def test_nested_non_deny_stays_allow():
    body = interpret_result(
        "PreToolUse",
        _frame({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "x"}}),
    )
    assert body.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
