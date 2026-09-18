"""coordinator_core/hooks/tests/test_arrival_w4_c8.py — the W4-C8 arrival
gate for the Agent/Bash dispatch guard family.

Subject: nine `hooks.*` ops landed by
`docs/plans/2026-09-18-doe-holds-no-scripts.md` § W4-C8 —
`preuse_bash_dispatch`, `guard_host_subagent_bash_ban`,
`guard_host_subagent_bash_spawn_shapes`, `guard_named_dispatch_tool_
restriction`, `enforce_agent_dispatch_mode`, `preuse_agent_dispatch`,
`preuse_skill_dispatch`, plus a new `hooks.block_unenumerated_agent_type`
op registration on an already-landed module, and
`nudge_foreground_agent_dispatch` (already landed — its own arrival gate
predates this row; only re-touched here indirectly through the fan-in
tests below).

Each op is exercised directly (no stdin/stdout, no subprocess — every op is
a same-repo, in-process `params: dict -> dict` coroutine per this package's
own `hooks.<name>` contract), covering the ordinary pass, the guard's own
fail-closed leg where one exists, and the fan-in isolation contract for the
two dispatchers whose registry legs are not all landed as of this dispatch.
"""

from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# hooks.preuse_bash_dispatch
# ---------------------------------------------------------------------------


def test_preuse_bash_dispatch_registers_op():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.preuse_bash_dispatch  # noqa: F401

    assert "hooks.preuse_bash_dispatch" in _REGISTRY


def test_preuse_bash_dispatch_fails_open_on_non_dict():
    from coordinator_core.hooks.preuse_bash_dispatch import _handler

    assert _run(_handler(None)) == {}


def test_preuse_bash_dispatch_allows_ordinary_command():
    from coordinator_core.hooks.preuse_bash_dispatch import _handler

    out = _run(
        _handler(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo hi"},
                "cwd": "/tmp",
                "session_id": "s",
            }
        )
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
    out = _run(mod._handler({"tool_name": "Bash", "tool_input": {"command": "x"}}))
    assert out == {}


# ---------------------------------------------------------------------------
# hooks.guard_host_subagent_bash_ban / hooks.guard_host_subagent_bash_spawn_shapes
# ---------------------------------------------------------------------------


def test_bash_ban_op_registers_and_reaches_check():
    from coordinator_core.ipc import _REGISTRY
    from coordinator_core.hooks.guard_host_subagent_bash_ban import _handler

    assert "hooks.guard_host_subagent_bash_ban" in _REGISTRY
    # No agent_id -> EM, out of scope -> no_advisory.
    assert _run(_handler({"tool_name": "Bash", "cwd": "/tmp"})) == {}


def test_bash_ban_op_fails_open_on_non_dict():
    from coordinator_core.hooks.guard_host_subagent_bash_ban import _handler

    assert _run(_handler(None)) == {}


def test_bash_ban_op_denies_under_deny_policy(tmp_path):
    from coordinator_core.hooks.guard_host_subagent_bash_ban import _handler

    (tmp_path / "coordinator.local.md").write_text(
        "---\nsubagent_bash_policy: deny\n---\nbody\n", encoding="utf-8"
    )
    out = _run(
        _handler(
            {
                "tool_name": "Bash",
                "cwd": str(tmp_path),
                "agent_id": "some-agent",
            }
        )
    )
    hso = out.get("hookSpecificOutput")
    assert hso is not None
    assert hso.get("permissionDecision") == "deny"


def test_bash_spawn_shapes_op_registers_and_fails_open_on_non_dict():
    from coordinator_core.ipc import _REGISTRY
    from coordinator_core.hooks.guard_host_subagent_bash_spawn_shapes import _handler

    assert "hooks.guard_host_subagent_bash_spawn_shapes" in _REGISTRY
    assert _run(_handler(None)) == {}


# ---------------------------------------------------------------------------
# hooks.block_unenumerated_agent_type — new op registration on landed module
# ---------------------------------------------------------------------------


def test_block_unenumerated_agent_type_op_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.block_unenumerated_agent_type  # noqa: F401

    assert "hooks.block_unenumerated_agent_type" in _REGISTRY


def test_block_unenumerated_agent_type_op_no_op_for_non_agent():
    from coordinator_core.hooks.block_unenumerated_agent_type import _handler

    assert _run(_handler({"tool_name": "Bash"})) == {}


# ---------------------------------------------------------------------------
# hooks.guard_named_dispatch_tool_restriction
# ---------------------------------------------------------------------------


def test_named_dispatch_restriction_op_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.guard_named_dispatch_tool_restriction  # noqa: F401

    assert "hooks.guard_named_dispatch_tool_restriction" in _REGISTRY


def test_named_dispatch_restriction_strips_named_explore():
    from coordinator_core.hooks.guard_named_dispatch_tool_restriction import _handler

    out = _run(
        _handler(
            {
                "tool_name": "Agent",
                "tool_input": {
                    "subagent_type": "Explore",
                    "name": "foo",
                    "prompt": "p",
                },
            }
        )
    )
    hso = out["hookSpecificOutput"]
    assert "name" not in hso["updatedInput"]
    assert hso["updatedInput"]["subagent_type"] == "Explore"


def test_named_dispatch_restriction_passes_unnamed_ordinary_type():
    from coordinator_core.hooks.guard_named_dispatch_tool_restriction import _handler

    out = _run(
        _handler(
            {
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "coordinator:executor", "prompt": "p"},
            }
        )
    )
    assert out == {}


# ---------------------------------------------------------------------------
# hooks.enforce_agent_dispatch_mode
# ---------------------------------------------------------------------------


def test_enforce_agent_dispatch_mode_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.enforce_agent_dispatch_mode  # noqa: F401

    assert "hooks.enforce_agent_dispatch_mode" in _REGISTRY


def test_enforce_agent_dispatch_mode_no_op_on_empty_payload():
    from coordinator_core.hooks.enforce_agent_dispatch_mode import _handler

    assert _run(_handler({})) == {}


def test_enforce_agent_dispatch_mode_elevates_mode():
    from coordinator_core.hooks.enforce_agent_dispatch_mode import _handler

    out = _run(
        _handler(
            {
                "permission_mode": "bypassPermissions",
                "tool_input": {"subagent_type": "coordinator:executor", "prompt": "p"},
            }
        )
    )
    hso = out["hookSpecificOutput"]
    assert hso["updatedInput"]["mode"] == "bypassPermissions"


def test_enforce_agent_dispatch_mode_teammate_name_path_segment_denied():
    from coordinator_core.hooks.enforce_agent_dispatch_mode import _handler

    out = _run(
        _handler(
            {
                "tool_input": {
                    "subagent_type": "coordinator:executor",
                    "name": "feature/auth-review",
                    "prompt": "p",
                }
            }
        )
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"


def test_enforce_agent_dispatch_mode_mode_escape_hatch(monkeypatch):
    from coordinator_core.hooks.enforce_agent_dispatch_mode import _handler

    monkeypatch.setenv("COORDINATOR_AGENT_MODE_OK", "1")
    out = _run(
        _handler(
            {
                "permission_mode": "bypassPermissions",
                "tool_input": {"subagent_type": "coordinator:executor", "prompt": "p"},
            }
        )
    )
    assert out == {}


# ---------------------------------------------------------------------------
# hooks.preuse_agent_dispatch — fan-in isolation
# ---------------------------------------------------------------------------


def test_preuse_agent_dispatch_registers():
    from coordinator_core.ipc import _REGISTRY
    import coordinator_core.hooks.preuse_agent_dispatch  # noqa: F401

    assert "hooks.preuse_agent_dispatch" in _REGISTRY


def test_preuse_agent_dispatch_no_op_on_non_dict():
    from coordinator_core.hooks.preuse_agent_dispatch import _handler

    assert _run(_handler(None)) == {}


def test_preuse_agent_dispatch_skips_absent_suite_invocation_leg_and_reaches_leg4():
    """Leg 1 (`block_dispatch_suite_invocation`) is not landed in this
    engine as of this dispatch — its import must fail, get isolated, and
    the fan-in must still reach leg 4's own decision rather than raising."""
    from coordinator_core.hooks.preuse_agent_dispatch import _handler

    out = _run(
        _handler(
            {
                "permission_mode": "bypassPermissions",
                "tool_input": {"subagent_type": "coordinator:executor", "prompt": "p"},
            }
        )
    )
    hso = out["hookSpecificOutput"]
    assert hso["updatedInput"]["mode"] == "bypassPermissions"


def test_preuse_agent_dispatch_first_deny_wins_over_leg4():
    """block_unenumerated_agent_type (leg 2) denies an unenumerated
    subagent_type before leg 4 (enforce_agent_dispatch_mode) ever runs."""
    from coordinator_core.hooks.preuse_agent_dispatch import _handler

    out = _run(
        _handler(
            {
                "tool_name": "Agent",
                "permission_mode": "bypassPermissions",
                "tool_input": {
                    "subagent_type": "totally-unenumerated-probe-type",
                    "prompt": "p",
                },
            }
        )
    )
    hso = out.get("hookSpecificOutput")
    assert hso is not None
    assert hso.get("permissionDecision") == "deny"


# ---------------------------------------------------------------------------
# hooks.preuse_skill_dispatch — fan-in isolation, no legs landed yet
# ---------------------------------------------------------------------------


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
