"""coordinator_core.hooks.tests.test_block_ungranted_opus_subagent -- coverage
for the PreToolUse(Agent) Opus/Fable persona-or-grant gate, and the seam it
composes into on `block_unenumerated_agent_type.check()`.

Spec backlink: state/audits/2026-09-18-why-the-opus-subagent-guard-did-not-fire.md
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.hooks.block_ungranted_opus_subagent as mod
import coordinator_core.hooks.block_unenumerated_agent_type as unenumerated_mod


def _agent_payload(
    subagent_type: str,
    *,
    model: str = "",
    transcript_path: str = "",
    payload_model: str = "",
    prompt: str = "do the thing",
) -> dict:
    tool_input: dict = {"subagent_type": subagent_type, "prompt": prompt}
    if model:
        tool_input["model"] = model
    payload: dict = {"tool_name": "Agent", "tool_input": tool_input}
    if transcript_path:
        payload["transcript_path"] = transcript_path
    if payload_model:
        payload["model"] = payload_model
    return payload


def _patch_pins(monkeypatch: pytest.MonkeyPatch, pins, reason=None) -> None:
    def _fake_resolve_model_pins(*, doe_root=None, home=None):
        del doe_root, home
        return (pins, reason)

    monkeypatch.setattr(mod, "resolve_model_pins", _fake_resolve_model_pins)


def _write_transcript(tmp_path: Path, *, model: str, extra_lines: "list[str]" = None) -> str:
    path = tmp_path / "transcript.jsonl"
    lines = list(extra_lines or [])
    lines.append(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "model": model}})
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# check() -- deny / pass, pins + transcript injected
# ---------------------------------------------------------------------------


def _assert_rewritten_to_sonnet(envelope, payload: dict) -> None:
    assert envelope is not None
    hso = envelope["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert hso["updatedInput"] == {**payload["tool_input"], "model": "sonnet"}
    assert "model" not in payload["tool_input"]  # a new object, never a mutation


def test_general_purpose_no_model_under_opus_parent_rewrites_to_sonnet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    payload = _agent_payload("general-purpose", transcript_path=transcript)
    envelope = mod.check(payload)
    _assert_rewritten_to_sonnet(envelope, payload)
    assert "general-purpose" in envelope["hookSpecificOutput"]["additionalContext"]


def test_general_purpose_explicit_model_opus_still_denies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    envelope = mod.check(_agent_payload("general-purpose", model="opus", transcript_path=transcript))
    assert envelope is not None
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "updatedInput" not in envelope["hookSpecificOutput"]


def test_fable_pinned_type_with_no_model_param_still_denies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {"some-type": {"model": "fable"}})
    transcript = _write_transcript(tmp_path, model="claude-sonnet-4-5")
    envelope = mod.check(_agent_payload("some-type", transcript_path=transcript))
    assert envelope is not None
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_general_purpose_explicit_model_sonnet_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    envelope = mod.check(
        _agent_payload("general-purpose", model="sonnet", transcript_path=transcript)
    )
    assert envelope is None


def test_general_purpose_explicit_model_fable_denies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-sonnet-4-5")
    envelope = mod.check(_agent_payload("general-purpose", model="fable", transcript_path=transcript))
    assert envelope is not None
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_sonnet_parent_inherit_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-sonnet-4-5")
    envelope = mod.check(_agent_payload("general-purpose", transcript_path=transcript))
    assert envelope is None


def test_unknown_parent_rewrites_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})
    payload = _agent_payload("general-purpose", transcript_path="")
    envelope = mod.check(payload)
    _assert_rewritten_to_sonnet(envelope, payload)
    assert "unresolved" in envelope["hookSpecificOutput"]["additionalContext"]


def test_opus_pinned_persona_passes_with_no_model_param(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:eng-director": {"model": "opus", "_source_path": "x"}},
    )
    assert mod.check(_agent_payload("coordinator:eng-director")) is None


def test_opus_pinned_persona_passes_with_explicit_model_opus(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:eng-director": {"model": "opus", "_source_path": "x"}},
    )
    assert mod.check(_agent_payload("coordinator:eng-director", model="opus")) is None


def test_fork_under_opus_parent_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PM ruling 2026-09-18: a fork is a new EM session, a named exception."""
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    assert mod.check(_agent_payload("fork", transcript_path=transcript)) is None
    assert mod.check(_agent_payload("fork", model="opus", transcript_path=transcript)) is None


def test_unresolved_roster_never_denies_sonnet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mod, "resolve_model_pins", lambda: (None, "roster missing"))
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    assert mod.check(_agent_payload("general-purpose", model="sonnet", transcript_path=transcript)) is None
    payload = _agent_payload("general-purpose", transcript_path=transcript)
    _assert_rewritten_to_sonnet(mod.check(payload), payload)


def test_grant_env_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_pins(monkeypatch, {})
    monkeypatch.setenv(mod._OVERRIDE_ENV, "1")
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    assert mod.check(_agent_payload("general-purpose", transcript_path=transcript)) is None
    assert mod.check(_agent_payload("fork", model="opus", transcript_path=transcript)) is None


def test_grant_env_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_pins(monkeypatch, {})
    monkeypatch.delenv(mod._OVERRIDE_ENV, raising=False)
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    envelope = mod.check(_agent_payload("general-purpose", transcript_path=transcript))
    assert envelope is not None


def test_non_agent_tool_name_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})
    payload = {"tool_name": "Bash", "tool_input": {"subagent_type": "general-purpose"}}
    assert mod.check(payload) is None


def test_absent_subagent_type_under_opus_parent_rewrites_to_sonnet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The harness runs an omitted type as general-purpose; the gate must too.
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-opus-5-5[1m]")
    payload = {
        "tool_name": "Agent",
        "tool_input": {"prompt": "do the thing"},
        "transcript_path": transcript,
    }
    envelope = mod.check(payload)
    _assert_rewritten_to_sonnet(envelope, payload)
    assert "subagent_type" not in envelope["hookSpecificOutput"]["updatedInput"]


def test_payload_level_model_field_used_before_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})
    envelope = mod.check(_agent_payload("general-purpose", payload_model="sonnet"))
    assert envelope is None


def test_sonnet_pinned_type_with_explicit_opus_override_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    # This module's own gate would deny this on its own merits (opus, no
    # persona pin) even without the sibling's pin-fidelity deny firing first.
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"model": "sonnet", "_source_path": "x"}},
    )
    envelope = mod.check(_agent_payload("coordinator:executor", model="opus"))
    assert envelope is not None
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Composition -- block_unenumerated_agent_type.check() chains both legs.
# ---------------------------------------------------------------------------


def test_composed_seam_rewrites_via_opus_gate_when_pin_leg_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fake_resolve_roster(*, doe_root=None, home=None):
        del doe_root, home
        return (frozenset({"general-purpose"}), None)

    monkeypatch.setattr(unenumerated_mod, "resolve_roster", _fake_resolve_roster)
    _patch_pins(monkeypatch, {})

    import coordinator_core.hooks.enforce_agent_model_pin as pin_mod

    monkeypatch.setattr(pin_mod, "resolve_model_pins", lambda **_: ({}, None))

    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "general-purpose", "prompt": "do the thing"},
        "transcript_path": transcript,
    }
    envelope = unenumerated_mod.check(payload)
    _assert_rewritten_to_sonnet(envelope, payload)


def test_composed_seam_rewrites_absent_subagent_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        unenumerated_mod,
        "resolve_roster",
        lambda **_: (unenumerated_mod._HARNESS_BUILTIN_TYPES, None),
    )
    _patch_pins(monkeypatch, {})

    import coordinator_core.hooks.enforce_agent_model_pin as pin_mod

    monkeypatch.setattr(pin_mod, "resolve_model_pins", lambda **_: ({}, None))

    transcript = _write_transcript(tmp_path, model="claude-opus-5-5[1m]")
    payload = {
        "tool_name": "Agent",
        "tool_input": {"prompt": "do the thing", "description": "x"},
        "transcript_path": transcript,
    }
    _assert_rewritten_to_sonnet(unenumerated_mod.check(payload), payload)


def test_composed_seam_pin_deny_short_circuits_before_opus_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fake_resolve_roster(*, doe_root=None, home=None):
        del doe_root, home
        return (frozenset({"coordinator:executor"}), None)

    monkeypatch.setattr(unenumerated_mod, "resolve_roster", _fake_resolve_roster)

    import coordinator_core.hooks.enforce_agent_model_pin as pin_mod

    monkeypatch.setattr(
        pin_mod,
        "resolve_model_pins",
        lambda **_: ({"coordinator:executor": {"model": "sonnet", "_source_path": "x"}}, None),
    )

    payload = {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "coordinator:executor",
            "model": "opus",
            "prompt": "do the thing",
        },
    }
    envelope = unenumerated_mod.check(payload)
    assert envelope is not None
    reason = envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert "sonnet" in reason and "opus" in reason
    # The pin-fidelity reason, not the opus-gate's own reason shape.
    assert "cost-and-role invariant" in reason


def test_composed_seam_passes_clean_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _fake_resolve_roster(*, doe_root=None, home=None):
        del doe_root, home
        return (frozenset({"general-purpose"}), None)

    monkeypatch.setattr(unenumerated_mod, "resolve_roster", _fake_resolve_roster)
    _patch_pins(monkeypatch, {})

    import coordinator_core.hooks.enforce_agent_model_pin as pin_mod

    monkeypatch.setattr(pin_mod, "resolve_model_pins", lambda **_: ({}, None))

    transcript = _write_transcript(tmp_path, model="claude-sonnet-4-5")
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "general-purpose", "prompt": "do the thing"},
        "transcript_path": transcript,
    }
    assert unenumerated_mod.check(payload) is None
