"""general_purpose_code_handoff_deny: a top-level general-purpose/fork code handoff is advisory only."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile
from coordinator_core.hooks import block_unenumerated_agent_type as gate
from coordinator_core.hooks import general_purpose_code_handoff_deny as mod
from coordinator_core.hooks.doc_data_extensions import _DOC_DATA_EXTENSIONS

CODE_PROMPT = "Edit coordinator_core/hooks/foo.py to fix the off-by-one in parse()."
SCOUT_PROMPT = "Read coordinator_core/hooks/foo.py and report how parse() works. Change no file."


def _payload(prompt: str = CODE_PROMPT, subagent_type=None, **extra) -> dict:
    tool_input = {"prompt": prompt}
    if subagent_type is not None:
        tool_input["subagent_type"] = subagent_type
    return {"tool_name": "Agent", "tool_input": tool_input, **extra}


def _call(payload: dict):
    stype = gate.resolve_subagent_type(payload["tool_input"])
    return mod.general_purpose_code_handoff_deny(payload, stype)


@pytest.fixture
def gate_alone(monkeypatch):
    """The gate with its sibling and later legs silenced, so only this guard speaks."""
    from coordinator_core.hooks import hand_dispatch_deny, unsized_writer_dispatch_deny

    monkeypatch.setattr(hand_dispatch_deny, "hand_dispatch_deny", lambda *_a, **_k: None)
    monkeypatch.setattr(
        unsized_writer_dispatch_deny, "unsized_writer_dispatch_deny", lambda *_a, **_k: None
    )
    monkeypatch.setattr(gate, "_gate_legs", lambda *_a, **_k: None)
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "strict")


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "strict")


def _hso(out) -> dict:
    return out["hookSpecificOutput"]


@pytest.mark.parametrize("stype", [None, "general-purpose"])
def test_untyped_code_edit_handoff_is_allowed_with_executor_advice(strict, stype):
    hso = _hso(_call(_payload(subagent_type=stype)))
    assert hso["permissionDecision"] == "allow"
    assert "coordinator:executor" in hso["additionalContext"]


def test_fork_code_edit_handoff_is_allowed_naming_sonnet_lane(gate_alone):
    hso = _hso(gate.check(_payload(subagent_type="fork")))
    assert hso["permissionDecision"] == "allow"
    assert "coordinator:executor" in hso["additionalContext"]
    assert "scratch" in hso["additionalContext"]


@pytest.mark.parametrize("stype", [None, "general-purpose", "fork"])
def test_read_only_scout_naming_code_path_is_never_denied(gate_alone, stype):
    out = gate.check(_payload(SCOUT_PROMPT, subagent_type=stype))
    assert out is None or _hso(out)["permissionDecision"] != "deny"


def test_general_purpose_advice_names_scratch_forensics_lane(strict):
    out = _call(_payload(subagent_type="general-purpose"))
    assert "scratch" in _hso(out)["additionalContext"]


@pytest.mark.parametrize(
    "prompt",
    ["Read coordinator_core/hooks/foo.py and summarize it.", "Edit docs/plans/x.md to fix typo."],
)
def test_fork_non_edit_dispatch_passes(strict, prompt):
    assert _call(_payload(prompt, subagent_type="fork")) is None


def test_nested_fork_passes(strict):
    assert _call(_payload(subagent_type="fork", agent_id="a1")) is None


def test_gate_check_never_denies_code_handoff(gate_alone):
    out = gate.check(_payload())
    assert _hso(out)["permissionDecision"] == "allow"


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


def test_off_level_silences_advisory(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "off")
    assert _call(_payload()) is None


def test_warn_level_returns_allow_with_context(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "warn")
    hso = _hso(_call(_payload()))
    assert hso["permissionDecision"] == "allow"
    assert hso["additionalContext"]


def test_off_level_passes(monkeypatch):
    monkeypatch.setattr(machine_profile, "guard_level", lambda _name: "off")
    assert _call(_payload()) is None
