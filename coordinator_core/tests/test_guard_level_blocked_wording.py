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
