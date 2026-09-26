
from __future__ import annotations

import asyncio

import pytest


def _run(result):
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_preuse_bash_dispatch_registers_op():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.preuse_bash_dispatch  # noqa: F401

    assert "hooks.preuse_bash_dispatch" in _REGISTRY


def test_preuse_bash_dispatch_fails_open_on_non_dict():
    from coordinator_core.hooks.preuse_bash_dispatch import _handler

    assert _handler(None) == {}


def test_preuse_bash_dispatch_allows_ordinary_command():
    from coordinator_core.hooks.preuse_bash_dispatch import _handler

    out = _handler(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "cwd": "/tmp",
            "session_id": "s",
        }
    )
    assert isinstance(out, dict)
    hso = out.get("hookSpecificOutput")
    if hso is not None:
        assert hso.get("permissionDecision") != "deny"


def test_preuse_bash_dispatch_fails_open_when_chain_raises(monkeypatch):
    from coordinator_core.hooks import preuse_bash_dispatch as mod

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    import coordinator_core.bash_guards.dispatch as dispatch_mod

    monkeypatch.setattr(dispatch_mod, "evaluate_payload_json", _boom)
    out = mod._handler({"tool_name": "Bash", "tool_input": {"command": "x"}})
    assert out != {}
    hso = out.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "allow"
    context = hso.get("additionalContext") or ""
    assert "could not be evaluated" in context
    assert "did not pass -- it did not run" in context
    assert "boom" in context


def test_preuse_bash_dispatch_missing_origin_worktree_surfaces_visibly(monkeypatch):
    from coordinator_core.hooks import preuse_bash_dispatch as mod

    def _raise_missing_routing_key(*a, **kw):
        raise ValueError(
            "op 'hooks.preuse_bash_dispatch' (scope='repo') requires "
            "_origin_worktree but it was absent or not a valid string."
        )

    import coordinator_core.bash_guards.dispatch as dispatch_mod

    monkeypatch.setattr(
        dispatch_mod, "evaluate_payload_json", _raise_missing_routing_key
    )
    out = mod._handler(
        {"tool_name": "Bash", "tool_input": {"command": "cd repo && git commit"}}
    )
    hso = out.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "allow"
    assert "_origin_worktree" in (hso.get("additionalContext") or "")


def _banned_command(tmp_path) -> str:
    return f"git worktree add {tmp_path / 'x'}"


def _wire_params(tmp_path, command):
    from coordinator_core.warm.hook_http import payload_from_event

    return {
        "payload": payload_from_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "cwd": str(tmp_path),
                "session_id": "s",
            }
        )
    }


def _decision(out):
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


def test_preuse_bash_dispatch_denies_through_the_envelope_the_doors_send(tmp_path):
    from coordinator_core.hooks.preuse_bash_dispatch import _handler

    assert _decision(_handler(_wire_params(tmp_path, _banned_command(tmp_path)))) == "deny"


def test_preuse_bash_dispatch_deny_and_allow_are_distinguishable(tmp_path):
    """A verdict surface that answers the same for both is worse than one that
    errors: it reads as a clean pass. Pinning the DIFFERENCE catches the whole
    class, including a future fail-open that keeps the deny leg working."""
    from coordinator_core.hooks.preuse_bash_dispatch import _handler

    denied = _handler(_wire_params(tmp_path, _banned_command(tmp_path)))
    allowed = _handler(_wire_params(tmp_path, "echo hi"))
    assert denied != allowed
    assert _decision(allowed) != "deny"


def test_preuse_bash_dispatch_still_reads_a_flat_payload(tmp_path):
    from coordinator_core.hooks.preuse_bash_dispatch import _handler

    flat = _wire_params(tmp_path, _banned_command(tmp_path))["payload"]
    assert _decision(_handler(flat)) == "deny"


def test_bash_ban_op_registers_and_reaches_check():
    from coordinator_core.ipc import _REGISTRY
    from coordinator_core.hooks.guard_host_subagent_bash_ban import _handler

    assert "hooks.guard_host_subagent_bash_ban" in _REGISTRY
    assert _handler({"tool_name": "Bash", "cwd": "/tmp"}) == {}


def test_bash_ban_op_fails_open_on_non_dict():
    from coordinator_core.hooks.guard_host_subagent_bash_ban import _handler

    assert _handler(None) == {}


def test_bash_ban_op_denies_under_deny_policy(tmp_path):
    from coordinator_core.hooks.guard_host_subagent_bash_ban import _handler

    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_policy: deny\n---\nbody\n", encoding="utf-8"
    )
    out = _handler(
        {
            "tool_name": "Bash",
            "cwd": str(tmp_path),
            "agent_id": "some-agent",
        }
    )
    hso = out.get("hookSpecificOutput")
    assert hso is not None
    assert hso.get("permissionDecision") == "deny"


def test_bash_spawn_shapes_op_registers_and_fails_open_on_non_dict():
    from coordinator_core.ipc import _REGISTRY
    from coordinator_core.hooks.guard_host_subagent_bash_spawn_shapes import _handler

    assert "hooks.guard_host_subagent_bash_spawn_shapes" in _REGISTRY
    assert _handler(None) == {}


def test_block_unenumerated_agent_type_op_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.block_unenumerated_agent_type  # noqa: F401

    assert "hooks.block_unenumerated_agent_type" in _REGISTRY


def test_block_unenumerated_agent_type_op_no_op_for_non_agent():
    from coordinator_core.hooks.block_unenumerated_agent_type import _handler

    assert _handler({"tool_name": "Bash"}) == {}


def test_named_dispatch_restriction_op_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.guard_named_dispatch_tool_restriction  # noqa: F401

    assert "hooks.guard_named_dispatch_tool_restriction" in _REGISTRY


def test_named_dispatch_restriction_strips_named_explore():
    from coordinator_core.hooks.guard_named_dispatch_tool_restriction import _handler

    out = _handler(
        {
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "Explore",
                "name": "foo",
                "prompt": "p",
            },
        }
    )
    hso = out["hookSpecificOutput"]
    assert "name" not in hso["updatedInput"]
    assert hso["updatedInput"]["subagent_type"] == "Explore"


def test_named_dispatch_restriction_passes_unnamed_ordinary_type():
    from coordinator_core.hooks.guard_named_dispatch_tool_restriction import _handler

    out = _handler(
        {
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "coordinator:executor", "prompt": "p"},
        }
    )
    assert out == {}


def test_named_dispatch_restriction_denies_through_the_wrapped_envelope():
    from coordinator_core.hooks.guard_named_dispatch_tool_restriction import _handler

    out = _handler(
        {
            "payload": {
                "tool_name": "Agent",
                "tool_input": {
                    "subagent_type": "Explore",
                    "name": "foo",
                    "prompt": "p",
                    "unrecognised_key": "x",
                },
            }
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_enforce_agent_dispatch_mode_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.enforce_agent_dispatch_mode  # noqa: F401

    assert "hooks.enforce_agent_dispatch_mode" in _REGISTRY


def test_enforce_agent_dispatch_mode_no_op_on_empty_payload():
    from coordinator_core.hooks.enforce_agent_dispatch_mode import _handler

    assert _handler({}) == {}


def test_enforce_agent_dispatch_mode_elevates_mode():
    from coordinator_core.hooks.enforce_agent_dispatch_mode import _handler

    out = _handler(
        {
            "permission_mode": "bypassPermissions",
            "tool_input": {"subagent_type": "coordinator:executor", "prompt": "p"},
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["updatedInput"]["mode"] == "bypassPermissions"


def test_enforce_agent_dispatch_mode_teammate_name_path_segment_denied():
    from coordinator_core.hooks.enforce_agent_dispatch_mode import _handler

    out = _handler(
        {
            "tool_input": {
                "subagent_type": "coordinator:executor",
                "name": "feature/auth-review",
                "prompt": "p",
            }
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_enforce_agent_dispatch_mode_mode_escape_hatch(monkeypatch):
    from coordinator_core.hooks.enforce_agent_dispatch_mode import _handler

    monkeypatch.setenv("COORDINATOR_AGENT_MODE_OK", "1")
    out = _handler(
        {
            "permission_mode": "bypassPermissions",
            "tool_input": {"subagent_type": "coordinator:executor", "prompt": "p"},
        }
    )
    assert out == {}


def test_preuse_agent_dispatch_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.preuse_agent_dispatch  # noqa: F401

    assert "hooks.preuse_agent_dispatch" in _REGISTRY


def test_preuse_agent_dispatch_no_op_on_non_dict():
    from coordinator_core.hooks.preuse_agent_dispatch import _handler

    assert _handler(None) == {}


def test_preuse_agent_dispatch_skips_absent_suite_invocation_leg_and_reaches_leg4():
    from coordinator_core.hooks.preuse_agent_dispatch import _handler

    out = _handler(
        {
            "permission_mode": "bypassPermissions",
            "tool_input": {"subagent_type": "coordinator:executor", "prompt": "p"},
        }
    )
    hso = out["hookSpecificOutput"]
    assert hso["updatedInput"]["mode"] == "bypassPermissions"


def test_preuse_agent_dispatch_first_deny_wins_over_leg4():
    from coordinator_core.hooks.preuse_agent_dispatch import _handler

    out = _handler(
        {
            "tool_name": "Agent",
            "permission_mode": "bypassPermissions",
            "tool_input": {
                "subagent_type": "totally-unenumerated-probe-type",
                "prompt": "p",
            },
        }
    )
    hso = out.get("hookSpecificOutput")
    assert hso is not None
    assert hso.get("permissionDecision") == "deny"


def test_preuse_skill_dispatch_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.preuse_skill_dispatch  # noqa: F401

    assert "hooks.preuse_skill_dispatch" in _REGISTRY


def test_preuse_skill_dispatch_no_op_on_unmatched_verb():
    from coordinator_core.hooks.preuse_skill_dispatch import _handler

    out = _run(
        _handler(
            {
                "tool_name": "Skill",
                "tool_input": {"command": "some-other-verb", "args": ""},
                "session_id": "s",
                "cwd": "/tmp",
            }
        )
    )
    assert out == {}


def test_preuse_skill_dispatch_degrades_silently_when_every_matched_leg_absent():
    """Every REGISTRY leg's owning module is a sibling row's writes:, not
    landed here — a matched verb must degrade to no_advisory(), never
    raise, since every leg import fails inside its own isolation."""
    from coordinator_core.hooks.preuse_skill_dispatch import _handler

    out = _run(
        _handler(
            {
                "tool_name": "Skill",
                "tool_input": {"command": "pickup", "args": ""},
                "session_id": "s",
                "cwd": "/tmp",
            }
        )
    )
    assert out == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
