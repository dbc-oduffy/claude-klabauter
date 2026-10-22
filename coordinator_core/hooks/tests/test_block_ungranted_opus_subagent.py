
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
    def _fake_resolve_model_pins(*, content_root=None, home=None):
        del content_root, home
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


def _assert_rewritten_to_sonnet(envelope, payload: dict) -> None:
    assert envelope is not None
    hso = envelope["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert hso["updatedInput"] == {**payload["tool_input"], "model": "sonnet"}
    assert "model" not in payload["tool_input"]


def _assert_explicit_rewritten_to_sonnet(envelope, payload: dict, requested: str) -> None:
    assert envelope is not None
    hso = envelope["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert hso["updatedInput"] == {**payload["tool_input"], "model": "sonnet"}
    assert payload["tool_input"]["model"] == requested
    assert "requested" in hso["additionalContext"]


def test_general_purpose_no_model_under_opus_parent_rewrites_to_sonnet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    payload = _agent_payload("general-purpose", transcript_path=transcript)
    envelope = mod.check(payload)
    _assert_rewritten_to_sonnet(envelope, payload)
    assert "general-purpose" in envelope["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("level", ["warn", "strict"])
def test_general_purpose_explicit_model_opus_rewrites_to_sonnet_at_every_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, level: str
) -> None:
    monkeypatch.setattr("coordinator_core.machine_profile.guard_level", lambda name: level)
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    payload = _agent_payload("general-purpose", model="opus", transcript_path=transcript)
    _assert_explicit_rewritten_to_sonnet(mod.check(payload), payload, "opus")


@pytest.mark.parametrize("profile", ["author", "consumer"])
def test_explicit_model_opus_rewrites_at_the_unset_default_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: str
) -> None:
    import coordinator_core.machine_profile as mp

    monkeypatch.setattr(mp, "_explicit", lambda key, levels: None)
    monkeypatch.setattr(mp, "machine_profile", lambda: profile)
    monkeypatch.setattr(mp, "_slot", lambda: {})
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-opus-4-1-20250805")
    payload = _agent_payload("general-purpose", model="opus", transcript_path=transcript)
    _assert_explicit_rewritten_to_sonnet(mod.check(payload), payload, "opus")


def test_fable_pinned_type_with_no_model_param_still_denies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {"some-type": {"model": "fable"}})
    transcript = _write_transcript(tmp_path, model="claude-sonnet-4-5")
    monkeypatch.setattr("coordinator_core.machine_profile.guard_level", lambda name: "warn")
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


def test_general_purpose_explicit_model_fable_rewrites_to_sonnet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-sonnet-4-5")
    payload = _agent_payload("general-purpose", model="fable", transcript_path=transcript)
    _assert_explicit_rewritten_to_sonnet(mod.check(payload), payload, "fable")


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


_SID = {"game-dev:staff-game-dev": {"model": "opus", "_source_path": "x"}}


@pytest.mark.parametrize(
    "prompt",
    [
        "Diagnose the two eye-render defects and implement host code plus tests.",
        "Fix the crash in the loader.",
        "Probe the cache and patch the stale entry.",
        "Implement the plan in docs/plans/x.md and fix the loader",
        "Patch per the review findings",
    ],
)
def test_opus_persona_execution_brief_denies_with_executor_pointer(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    _patch_pins(monkeypatch, _SID)
    envelope = mod.check(_agent_payload("game-dev:staff-game-dev", prompt=prompt))
    assert envelope is not None
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "coordinator:executor" in hso["permissionDecisionReason"]


@pytest.mark.parametrize(
    "prompt",
    [
        "Review the plan and give a verdict.",
        "Critique this design; fix nothing.",
        "Rule on whether we should implement the cache.",
        "Evaluating the patch for the loader fix.",
        "Ruling on whether to fix the crash.",
        "do the thing",
        "",
    ],
)
def test_opus_persona_judgment_or_ambiguous_brief_passes(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    _patch_pins(monkeypatch, _SID)
    assert mod.check(_agent_payload("game-dev:staff-game-dev", prompt=prompt)) is None


@pytest.mark.parametrize(
    "persona",
    [
        "coordinator:apm",
        "coordinator:vp-product",
        "coordinator:blitz-em",
        "coordinator:subtractive-adjudicator",
    ],
)
def test_ruling_persona_passes_an_amend_brief_with_execution_verbs(
    monkeypatch: pytest.MonkeyPatch, persona: str
) -> None:
    _patch_pins(monkeypatch, {persona: {"model": "opus"}})
    prompt = "Amend the executing plan in place: add a tier dimension, fix two rows, implement R10."
    assert mod.check(_agent_payload(persona, prompt=prompt)) is None


@pytest.mark.parametrize("persona", ["coordinator:plan-author", "coordinator:roadmap-planner"])
def test_plan_writer_mechanical_brief_is_rewritten_to_sonnet(
    monkeypatch: pytest.MonkeyPatch, persona: str
) -> None:
    _patch_pins(monkeypatch, {persona: {"model": "opus"}})
    prompt = "Amend the executing plan in place: add a tier dimension, fix two rows, implement R10."
    payload = _agent_payload(persona, prompt=prompt)
    envelope = mod.check(payload)
    _assert_rewritten_to_sonnet(envelope, payload)
    context = envelope["hookSpecificOutput"]["additionalContext"]
    assert persona in context and "'fix'" in context


_REAL_BRIEF_OPENING = (
    "Make each plan pass the mise-prep-gate. Fix classes: spine rows, body headings."
)


def _pointer_prompt(path: Path) -> str:
    return f"Read your full brief at {path.as_posix()} and follow it exactly."


def test_plan_author_pointer_brief_with_execution_vocab_is_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    persona = "coordinator:plan-author"
    _patch_pins(monkeypatch, {persona: {"model": "opus"}})
    brief = tmp_path / "r2-brief-01.md"
    brief.write_text(_REAL_BRIEF_OPENING, encoding="utf-8")
    payload = _agent_payload(persona, prompt=_pointer_prompt(brief))
    _assert_rewritten_to_sonnet(mod.check(payload), payload)


@pytest.mark.parametrize("opening", ["Plan the migration. Fix nothing yet.", "Author a plan for X; fix R3."])
def test_plan_author_pointer_brief_opening_with_plan_or_author_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, opening: str
) -> None:
    persona = "coordinator:plan-author"
    _patch_pins(monkeypatch, {persona: {"model": "opus"}})
    brief = tmp_path / "brief.md"
    brief.write_text(opening, encoding="utf-8")
    assert mod.check(_agent_payload(persona, prompt=_pointer_prompt(brief))) is None


def test_authoring_verb_does_not_widen_executor_persona_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pins(monkeypatch, _SID)
    envelope = mod.check(_agent_payload("game-dev:staff-game-dev", prompt="Author the fix."))
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_apm_pointer_brief_with_fix_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    persona = "coordinator:apm"
    _patch_pins(monkeypatch, {persona: {"model": "opus"}})
    brief = tmp_path / "brief.md"
    brief.write_text(_REAL_BRIEF_OPENING, encoding="utf-8")
    assert mod.check(_agent_payload(persona, prompt=_pointer_prompt(brief))) is None


def test_pointer_to_missing_file_classifies_on_prompt_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    persona = "coordinator:plan-author"
    _patch_pins(monkeypatch, {persona: {"model": "opus"}})
    assert mod.check(_agent_payload(persona, prompt=_pointer_prompt(tmp_path / "gone.md"))) is None
    missing_fix = f"Fix it per {(tmp_path / 'gone.md').as_posix()}"
    payload = _agent_payload(persona, prompt=missing_fix)
    _assert_rewritten_to_sonnet(mod.check(payload), payload)


def test_executor_persona_pointer_brief_with_execution_vocab_denied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, _SID)
    brief = tmp_path / "brief.md"
    brief.write_text(_REAL_BRIEF_OPENING, encoding="utf-8")
    envelope = mod.check(_agent_payload("game-dev:staff-game-dev", prompt=_pointer_prompt(brief)))
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_opus_persona_execution_brief_description_field_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pins(monkeypatch, _SID)
    payload = _agent_payload("game-dev:staff-game-dev", prompt="see attached")
    payload["tool_input"]["description"] = "Fix the eye shader"
    assert mod.check(payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_opus_persona_execution_brief_grant_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pins(monkeypatch, _SID)
    monkeypatch.setenv(mod._OVERRIDE_ENV, "1")
    assert mod.check(_agent_payload("game-dev:staff-game-dev", prompt="Fix the crash.")) is None
    monkeypatch.delenv(mod._OVERRIDE_ENV)
    assert mod.check(_agent_payload("game-dev:staff-game-dev", prompt="Fix the crash.")) is not None


def test_fork_under_opus_parent_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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


def test_sonnet_pinned_type_with_explicit_opus_override_rewrites(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(
        monkeypatch,
        {"coordinator:executor": {"model": "sonnet", "_source_path": "x"}},
    )
    payload = _agent_payload("coordinator:executor", model="opus")
    _assert_explicit_rewritten_to_sonnet(mod.check(payload), payload, "opus")


def test_composed_seam_rewrites_via_opus_gate_when_pin_leg_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _fake_resolve_roster(*, content_root=None, home=None):
        del content_root, home
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
    def _fake_resolve_roster(*, content_root=None, home=None):
        del content_root, home
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
    assert "cost-and-role invariant" in reason


def test_composed_seam_passes_clean_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _fake_resolve_roster(*, content_root=None, home=None):
        del content_root, home
        return (frozenset({"general-purpose"}), None)

    monkeypatch.setattr(unenumerated_mod, "resolve_roster", _fake_resolve_roster)
    _patch_pins(monkeypatch, {})

    import coordinator_core.hooks.enforce_agent_model_pin as pin_mod

    monkeypatch.setattr(pin_mod, "resolve_model_pins", lambda **_: ({}, None))

    transcript = _write_transcript(tmp_path, model="claude-sonnet-4-5")
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "general-purpose", "model": "sonnet", "prompt": "do the thing"},
        "transcript_path": transcript,
    }
    assert unenumerated_mod.check(payload) is None


def test_fork_execution_brief_on_opus_parent_denies_with_executor_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pins(monkeypatch, {})
    payload = _agent_payload("fork", prompt="Fix plugin-band TU include/define gaps")
    payload["model"] = "claude-opus-5-5"
    hso = mod.check(payload)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "coordinator:executor" in hso["permissionDecisionReason"]


def test_fork_execution_brief_on_sonnet_parent_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})
    payload = _agent_payload("fork", prompt="Fix the loader")
    payload["model"] = "claude-sonnet-5-5"
    assert mod.check(payload) is None


def test_fork_non_execution_brief_on_opus_parent_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pins(monkeypatch, {})
    payload = _agent_payload("fork", prompt="Survey where the loader is called and report.")
    payload["model"] = "claude-opus-5-5"
    assert mod.check(payload) is None


@pytest.mark.parametrize(
    "prompt",
    [
        "Questions: does the repro verify the fix? Give verdicts with file:line evidence. "
        "Probe the cache path. Write findings only.",
        "Check the probe results; review only, do not edit anything.",
        "Go through commit 1f533cd05a and judge whether the fix is sound.",
        "Second opinion on the diff abc1234: is the patch correct? Review commit.",
    ],
)
def test_findings_only_or_commit_review_brief_passes(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    _patch_pins(monkeypatch, _SID)
    assert mod.check(_agent_payload("game-dev:staff-game-dev", prompt=prompt)) is None


_EXEC_VOCAB_BRIEF = "Execute the chunk: edit the loader, merge the branch. Commit scoped."


def test_fork_execute_edit_merge_commit_scoped_brief_denies_naming_phrase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-opus-5-5")
    envelope = mod.check(_agent_payload("fork", prompt=_EXEC_VOCAB_BRIEF, transcript_path=transcript))
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "'Execute the'" in hso["permissionDecisionReason"]


def test_persona_execute_edit_merge_commit_scoped_brief_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pins(monkeypatch, _SID)
    envelope = mod.check(_agent_payload("game-dev:staff-game-dev", prompt=_EXEC_VOCAB_BRIEF))
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "prompt", ["Merge the branches.", "Edit src/loader.py.", "Commit scoped."]
)
def test_each_new_verb_alone_is_execution(prompt: str) -> None:
    assert mod._is_execution_brief({"prompt": prompt})


@pytest.mark.parametrize(
    "prompt",
    [
        "Review the merge of 1f533cd05a",
        "Check the merge order; findings only, do not edit",
        "Plan the merge order for the three branches",
        "Open the editor and the edition notes, then report",
    ],
)
def test_new_vocabulary_negative_controls_pass(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    _patch_pins(monkeypatch, _SID)
    assert mod.check(_agent_payload("game-dev:staff-game-dev", prompt=prompt)) is None


def test_partial_do_not_edit_does_not_shield_commit_scoped_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pins(monkeypatch, _SID)
    prompt = "Execute the rows; do NOT edit the disposition. Commit scoped."
    envelope = mod.check(_agent_payload("game-dev:staff-game-dev", prompt=prompt))
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_plan_author_persona_edit_the_plan_is_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    persona = "coordinator:plan-author"
    _patch_pins(monkeypatch, {persona: {"model": "opus"}})
    payload = _agent_payload(persona, prompt="Edit the plan and merge the rows.")
    _assert_rewritten_to_sonnet(mod.check(payload), payload)


def test_fork_exec_vocab_brief_on_sonnet_parent_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_pins(monkeypatch, {})
    transcript = _write_transcript(tmp_path, model="claude-sonnet-5-5")
    assert mod.check(_agent_payload("fork", prompt=_EXEC_VOCAB_BRIEF, transcript_path=transcript)) is None


@pytest.mark.parametrize(
    "prompt", ["Fix the loader and commit.", "Inspect commit abc1234, then fix it and commit."]
)
def test_execution_brief_still_blocks_and_names_phrase(
    monkeypatch: pytest.MonkeyPatch, prompt: str
) -> None:
    _patch_pins(monkeypatch, _SID)
    envelope = mod.check(_agent_payload("game-dev:staff-game-dev", prompt=prompt))
    reason = envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert "'Fix'" in reason or "'fix'" in reason
    assert mod._OVERRIDE_ENV not in reason
