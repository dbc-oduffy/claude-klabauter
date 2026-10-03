"""A warn-level downgrade must not claim it blocked."""
from coordinator_core import machine_profile


def test_warn_downgrade_rewrites_blocked_prefix(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda name: "warn")
    deny = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "BLOCKED: 'find' scans the disk.",
    }}
    out = machine_profile.apply_guard_level("some-guard-xyz", deny)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "BLOCKED:" not in ctx
    assert "would be blocked at strict level: 'find' scans the disk." in ctx
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_warn_downgrade_keeps_per_path_lines_under_header(monkeypatch):
    from coordinator_core.bash_guards.dispatch_checks import (
        _format_batched_scope_denial,
    )

    monkeypatch.setattr(machine_profile, "guard_level", lambda name: "warn")
    entries = [
        {"path": "a/one.ts", "kind": "unclaimed", "text": "unclaimed: one"},
        {"path": "b/two.ts", "kind": "contested", "text": "contested: two"},
    ]
    deny = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _format_batched_scope_denial(entries),
    }}
    ctx = machine_profile.apply_guard_level("validate-commit", deny)[
        "hookSpecificOutput"]["additionalContext"]
    for e in entries:
        assert e["path"] in ctx and e["text"] in ctx
    assert ctx.index("two.ts") < ctx.index("Stricter:")
