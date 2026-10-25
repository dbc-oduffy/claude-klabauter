"""general_purpose_code_handoff_deny: a top-level general-purpose code-edit handoff is refused."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.hooks import block_unenumerated_agent_type as gate
from coordinator_core.hooks import general_purpose_code_handoff_deny as mod
from coordinator_core.hooks.doc_data_extensions import _DOC_DATA_EXTENSIONS

CODE_PROMPT = "Edit coordinator_core/hooks/foo.py to fix the off-by-one in parse()."


def _payload(prompt: str = CODE_PROMPT, subagent_type=None, **extra) -> dict:
    tool_input = {"prompt": prompt}
    if subagent_type is not None:
        tool_input["subagent_type"] = subagent_type
    return {"tool_name": "Agent", "tool_input": tool_input, **extra}


def _call(payload: dict):
    stype = gate.resolve_subagent_type(payload["tool_input"])
    return mod.general_purpose_code_handoff_deny(payload, stype)


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "strict")


def _decision(out) -> str:
    return out["hookSpecificOutput"]["permissionDecision"]


@pytest.mark.parametrize("stype", [None, "general-purpose"])
def test_untyped_code_edit_handoff_is_denied_naming_executor(strict, stype):
    out = _call(_payload(subagent_type=stype))
    assert _decision(out) == "deny"
    assert "coordinator:executor" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_fork_code_edit_handoff_is_denied_naming_sonnet_lane(strict):
    out = gate.check(_payload(subagent_type="fork"))
    assert _decision(out) == "deny"
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "coordinator:executor" in reason
    assert "scratch" in reason


def test_general_purpose_refusal_names_scratch_forensics_lane(strict):
    out = _call(_payload(subagent_type="general-purpose"))
    assert "scratch" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "prompt",
    ["Read coordinator_core/hooks/foo.py and summarize it.", "Edit docs/plans/x.md to fix typo."],
)
def test_fork_non_edit_dispatch_passes(strict, prompt):
    assert _call(_payload(prompt, subagent_type="fork")) is None


def test_nested_fork_passes(strict):
    assert _call(_payload(subagent_type="fork", agent_id="a1")) is None


def test_gate_check_denies_before_roster(strict):
    out = gate.check(_payload())
    assert _decision(out) == "deny"


def test_typed_executor_passes(strict):
    assert _call(_payload(subagent_type="coordinator:executor")) is None


def test_nested_dispatch_passes(strict):
    assert _call(_payload(agent_id="a1")) is None


def test_non_agent_tool_passes(strict):
    payload = _payload()
    payload["tool_name"] = "Task"
    assert mod.general_purpose_code_handoff_deny(payload, "general-purpose") is None


@pytest.mark.parametrize(
    "prompt",
    [
        "Edit docs/plans/x.md to fix the typo.",
        "Change state/foo.yaml and add a key.",
        "Read coordinator_core/hooks/foo.py and summarize it.",
        "Fix the bug somewhere in the parser.",
        "Edit https://example.com/a.py please",
        "Add e.g. a note",
    ],
)
def test_doc_data_or_no_verb_or_no_path_passes(strict, prompt):
    assert _call(_payload(prompt)) is None


def test_doc_data_extensions_never_count_as_code():
    assert not (mod._CODE_EXTENSIONS & _DOC_DATA_EXTENSIONS)


def test_warn_level_returns_allow_with_context(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "warn")
    hso = _call(_payload())["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["additionalContext"]


def test_off_level_passes(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "off")
    assert _call(_payload()) is None
