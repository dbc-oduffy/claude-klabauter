"""An off-list deny becomes one nudge line that never claims it blocked."""
from coordinator_core import machine_profile


def _deny(reason):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def test_nudge_drops_blocked_prefix_and_allows():
    out = machine_profile.apply_guard_level(
        "some-guard-xyz", _deny("BLOCKED: 'find' scans the disk.")
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["additionalContext"] == "Advisory: 'find' scans the disk."


def test_nudge_is_one_line_with_every_path_and_no_override_key():
    from coordinator_core.bash_guards.dispatch_checks import (
        _format_batched_scope_denial,
    )

    entries = [
        {"path": "a/one.ts", "kind": "unclaimed", "text": "unclaimed: one"},
        {"path": "b/two.ts", "kind": "contested", "text": "contested: two"},
    ]
    out = machine_profile.apply_guard_level(
        "validate-commit", _deny(_format_batched_scope_denial(entries))
    )
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "\n" not in ctx
    assert ctx.startswith(machine_profile.ADVISORY_PREFIX)
    assert "machine-local" not in ctx and "Stricter:" not in ctx


def test_stop_event_nudge_is_a_system_message():
    deny = {"decision": "block", "reason": "BLOCKED: stop is premature."}
    out = machine_profile.apply_guard_level("some-stop-guard", deny)
    assert out == {"systemMessage": "Advisory: stop is premature.", "guardLevel": "log"}
