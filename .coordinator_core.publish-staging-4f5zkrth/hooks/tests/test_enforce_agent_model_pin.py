"""coordinator_core.hooks.tests.test_enforce_agent_model_pin -- coverage for
the PreToolUse(Agent) model/effort pin-enforcement guard, and the seam it
composes into on `block_unenumerated_agent_type.check()`.

Spec backlink: cross-repo/inbox/2026-08-11-project-rag-em-agent-model-pin-silently-overridable.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.hooks.block_unenumerated_agent_type as unenumerated_mod
import coordinator_core.hooks.enforce_agent_model_pin as mod


def _write_agent_md(doe_root: Path, name: str, *, model: str = "", effort: str = "") -> None:
    agents_dir = doe_root / "coordinator" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = [f"name: {name}"]
    if model:
        fm_lines.append(f"model: {model}")
    if effort:
        fm_lines.append(f"effort: {effort}")
    body = "---\n" + "\n".join(fm_lines) + "\n---\nbody\n"
    (agents_dir / f"{name}.md").write_text(body, encoding="utf-8")


def _agent_payload(subagent_type: str, model: str = "", effort: str = "", prompt: str = "do the thing") -> dict:
    tool_input: dict = {"subagent_type": subagent_type, "prompt": prompt}
    if model:
        tool_input["model"] = model
    if effort:
        tool_input["effort"] = effort
    return {"tool_name": "Agent", "tool_input": tool_input}


def _patch_pins(monkeypatch: pytest.MonkeyPatch, pins, reason=None) -> None:
    def _fake_resolve_model_pins(*, doe_root=None):
        del doe_root
        return (pins, reason)

    monkeypatch.setattr(mod, "resolve_model_pins", _fake_resolve_model_pins)


# ---------------------------------------------------------------------------
# check() -- deny / advisory / silent-pass, pins injected via monkeypatch
# ---------------------------------------------------------------------------


def test_pinned_sonnet_plus_model_opus_denies_naming_both(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"model": "sonnet", "_source_path": "test-fixture:/coordinator/agents/executor.md"}},
    )
    envelope = mod.check(_agent_payload("coordinator:executor", model="opus"))
    assert envelope is not None
    reason = envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sonnet" in reason
    assert "opus" in reason
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pinned_sonnet_plus_model_haiku_passes_never_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"model": "sonnet", "_source_path": "test-fixture:/coordinator/agents/executor.md"}},
    )
    envelope = mod.check(_agent_payload("coordinator:executor", model="haiku"))
    assert envelope is None or envelope["hookSpecificOutput"].get("permissionDecision") != "deny"


def test_pinned_sonnet_plus_model_sonnet_passes_silently_inert_control(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"model": "sonnet", "_source_path": "test-fixture:/coordinator/agents/executor.md"}},
    )
    assert mod.check(_agent_payload("coordinator:executor", model="sonnet")) is None


def test_no_model_no_effort_passes_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"model": "sonnet", "_source_path": "test-fixture:/coordinator/agents/executor.md"}},
    )
    assert mod.check(_agent_payload("coordinator:executor")) is None


def test_effort_low_pinned_plus_effort_high_denies_effort_axis_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"effort": "low", "_source_path": "test-fixture:/coordinator/agents/executor.md"}},
    )
    envelope = mod.check(_agent_payload("coordinator:executor", effort="high"))
    assert envelope is not None
    reason = envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert "low" in reason
    assert "high" in reason
    assert "model" not in reason.lower().split("pins", 1)[0]


def test_both_axes_violated_single_deny_naming_both(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {
            "coordinator:executor": {
                "model": "sonnet",
                "effort": "low",
                "_source_path": "test-fixture:/coordinator/agents/executor.md",
            }
        },
    )
    envelope = mod.check(_agent_payload("coordinator:executor", model="opus", effort="high"))
    assert envelope is not None
    reason = envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert "model" in reason and "sonnet" in reason and "opus" in reason
    assert "effort" in reason and "low" in reason and "high" in reason


def test_fork_type_with_model_opus_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {"coordinator:executor": {"model": "sonnet", "_source_path": "x"}})
    assert mod.check(_agent_payload("fork", model="opus")) is None


def test_unpinned_type_with_model_opus_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {"coordinator:executor": {"model": "sonnet", "_source_path": "x"}})
    assert mod.check(_agent_payload("Explore", model="opus")) is None
    assert mod.check(_agent_payload("general-purpose", model="opus")) is None


def test_model_fable_against_sonnet_pin_denies_via_unorderable_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"model": "sonnet", "_source_path": "test-fixture:/coordinator/agents/executor.md"}},
    )
    envelope = mod.check(_agent_payload("coordinator:executor", model="fable"))
    assert envelope is not None
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_override_env_var_passes_a_violating_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"model": "sonnet", "_source_path": "test-fixture:/coordinator/agents/executor.md"}},
    )
    monkeypatch.setenv(mod._OVERRIDE_ENV, "1")
    assert mod.check(_agent_payload("coordinator:executor", model="opus")) is None


def test_non_agent_tool_name_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})
    payload = {"tool_name": "Bash", "tool_input": {"subagent_type": "coordinator:executor", "model": "opus"}}
    assert mod.check(payload) is None


def test_absent_subagent_type_out_of_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})
    payload = {"tool_name": "Agent", "tool_input": {"model": "opus"}}
    assert mod.check(payload) is None


# ---------------------------------------------------------------------------
# Composition -- block_unenumerated_agent_type.check() delegates after its
# own enumeration verdict, never before.
# ---------------------------------------------------------------------------


def test_unenumerated_type_denies_with_enumeration_reason_not_pin_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_resolve_roster(*, doe_root=None, home=None):
        del doe_root, home
        return (frozenset({"coordinator:executor"}), None)

    monkeypatch.setattr(unenumerated_mod, "resolve_roster", _fake_resolve_roster)
    _patch_pins(monkeypatch, {"coordinator:executor": {"model": "sonnet", "_source_path": "x"}})

    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "hookprobe-invented", "model": "opus", "prompt": "do the thing"},
    }
    envelope = unenumerated_mod.check(payload)
    assert envelope is not None
    reason = envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert "not on the enumerated roster" in reason
    assert "sonnet" not in reason


def test_enumerated_type_with_violating_model_denies_via_composed_pin_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_resolve_roster(*, doe_root=None, home=None):
        del doe_root, home
        return (frozenset({"coordinator:executor"}), None)

    monkeypatch.setattr(unenumerated_mod, "resolve_roster", _fake_resolve_roster)
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"model": "sonnet", "_source_path": "test-fixture:/coordinator/agents/executor.md"}},
    )

    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "coordinator:executor", "model": "opus", "prompt": "do the thing"},
    }
    envelope = unenumerated_mod.check(payload)
    assert envelope is not None
    reason = envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sonnet" in reason and "opus" in reason


# ---------------------------------------------------------------------------
# resolve_model_pins() -- real frontmatter walk, own tmp_path fixture.
# ---------------------------------------------------------------------------


def test_resolve_model_pins_reads_model_and_effort_from_frontmatter(tmp_path: Path) -> None:
    doe_root = tmp_path / "doe-claude"
    _write_agent_md(doe_root, "executor", model="sonnet", effort="low")
    _write_agent_md(doe_root, "code-reviewer", model="haiku")
    _write_agent_md(doe_root, "unpinned-agent")

    pins, reason = unenumerated_mod.resolve_model_pins(doe_root=str(doe_root))
    assert reason is None
    assert pins is not None
    assert pins["coordinator:executor"]["model"] == "sonnet"
    assert pins["coordinator:executor"]["effort"] == "low"
    assert pins["coordinator:code-reviewer"]["model"] == "haiku"
    assert "coordinator:unpinned-agent" not in pins


def test_resolve_model_pins_fails_closed_on_missing_agents_dir(tmp_path: Path) -> None:
    doe_root = tmp_path / "doe-claude"
    pins, reason = unenumerated_mod.resolve_model_pins(doe_root=str(doe_root))
    assert pins is None
    assert reason is not None
    assert "MISSING ENTIRELY" in reason


# ---------------------------------------------------------------------------
# Regression -- resolve_roster()'s own shape/contents are unaffected by the
# `_load_agents_roster` -> `_scan_agents_frontmatter` refactor (§2 hard
# constraint: resolve_roster's signature and return type must not change).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# resolve_model_pins() -- plugin leg (c), synthetic fixture trees only --
# see spec "Use synthetic fixture trees for the plugin layouts -- do not
# assert against this machine's live ~/.claude/plugins contents".
# ---------------------------------------------------------------------------


def _write_plugin_agent_md(home: Path, rel_path: str, name: str, *, model: str = "", effort: str = "") -> None:
    fm_lines = [f"name: {name}"]
    if model:
        fm_lines.append(f"model: {model}")
    if effort:
        fm_lines.append(f"effort: {effort}")
    body = "---\n" + "\n".join(fm_lines) + "\n---\nbody\n"
    path = home / ".claude" / "plugins" / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_plugin_keyed_pin_via_full_resolve_model_pins(tmp_path: Path) -> None:
    doe_root = tmp_path / "doe-claude"
    home = tmp_path / "home"
    _write_agent_md(doe_root, "executor", model="sonnet")
    _write_plugin_agent_md(home, "game-dev/agents/staff-game-dev.md", "staff-game-dev", model="sonnet")

    pins, reason = unenumerated_mod.resolve_model_pins(doe_root=str(doe_root), home=str(home))
    assert reason is None
    assert pins is not None
    assert pins["game-dev:staff-game-dev"]["model"] == "sonnet"

    import coordinator_core.hooks.enforce_agent_model_pin as pin_mod

    monkeypatch_target = pin_mod.resolve_model_pins

    def _fake(*, doe_root=None):
        del doe_root
        return pins, None

    pin_mod.resolve_model_pins = _fake
    try:
        envelope = pin_mod.check(_agent_payload("game-dev:staff-game-dev", model="opus"))
        assert envelope is not None
        assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"

        # INERT CONTROL -- required: same pin, matching value, must pass silently.
        assert pin_mod.check(_agent_payload("game-dev:staff-game-dev", model="sonnet")) is None
    finally:
        pin_mod.resolve_model_pins = monkeypatch_target


def test_plugin_leg_model_inherit_declared_is_not_a_pin(tmp_path: Path) -> None:
    doe_root = tmp_path / "doe-claude"
    home = tmp_path / "home"
    _write_agent_md(doe_root, "executor")
    _write_plugin_agent_md(home, "game-dev/agents/inherits.md", "inherits", model="inherit")

    pins, reason = unenumerated_mod.resolve_model_pins(doe_root=str(doe_root), home=str(home))
    assert reason is None
    assert pins is not None
    assert "game-dev:inherits" not in pins


def test_plugin_leg_unorderable_declared_model_is_not_a_pin(tmp_path: Path) -> None:
    doe_root = tmp_path / "doe-claude"
    home = tmp_path / "home"
    _write_agent_md(doe_root, "executor")
    _write_plugin_agent_md(home, "game-dev/agents/typo.md", "typo", model="gpt4")

    pins, reason = unenumerated_mod.resolve_model_pins(doe_root=str(doe_root), home=str(home))
    assert reason is None
    assert pins is not None
    assert "game-dev:typo" not in pins


def test_plugin_leg_empty_declared_value_is_not_a_pin(tmp_path: Path) -> None:
    doe_root = tmp_path / "doe-claude"
    home = tmp_path / "home"
    _write_agent_md(doe_root, "executor")
    path = home / ".claude" / "plugins" / "game-dev" / "agents" / "blank.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: blank\nmodel: \" \"\n---\nbody\n", encoding="utf-8")

    pins, reason = unenumerated_mod.resolve_model_pins(doe_root=str(doe_root), home=str(home))
    assert reason is None
    assert pins is not None
    assert "game-dev:blank" not in pins


def test_plugin_pin_under_pre_refresh_snapshots_not_resolved(tmp_path: Path) -> None:
    doe_root = tmp_path / "doe-claude"
    home = tmp_path / "home"
    _write_agent_md(doe_root, "executor")
    _write_plugin_agent_md(
        home,
        "_pre-refresh-snapshots/game-dev/agents/deleted-ghost.md",
        "deleted-ghost",
        model="sonnet",
    )

    pins, reason = unenumerated_mod.resolve_model_pins(doe_root=str(doe_root), home=str(home))
    assert reason is None
    assert pins is not None
    assert "game-dev:deleted-ghost" not in pins


def test_unreadable_absent_plugins_dir_leaves_leg_b_pins_intact(tmp_path: Path) -> None:
    doe_root = tmp_path / "doe-claude"
    home = tmp_path / "home-with-no-claude-dir"
    _write_agent_md(doe_root, "executor", model="sonnet")

    pins, reason = unenumerated_mod.resolve_model_pins(doe_root=str(doe_root), home=str(home))
    assert reason is None
    assert pins is not None
    assert pins["coordinator:executor"]["model"] == "sonnet"


def test_resolve_roster_unaffected_by_agents_roster_refactor(tmp_path: Path) -> None:
    doe_root = tmp_path / "doe-claude"
    _write_agent_md(doe_root, "executor", model="sonnet")
    policy_dir = doe_root / "coordinator"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "subagent-sandbox-policy.yaml").write_text(
        "report_sidecar:\n  - coordinator:executor\n"
        "report_type_map:\n  coordinator:executor: run-report\n"
        "contract_blocks:\n  coordinator:executor: []\n"
        "dispatch_tier:\n  coordinator:executor: review-execution\n",
        encoding="utf-8",
    )
    roster, reason = unenumerated_mod.resolve_roster(doe_root=str(doe_root), home=None)
    assert reason is None
    assert isinstance(roster, frozenset)
    assert "coordinator:executor" in roster
    assert "Explore" in roster
