"""The pre-sized gate agent's stage act covers an S-arm resume: the 44e645 gate return stages, a plain S does not."""

from __future__ import annotations

import json
import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit.ask_contract import OP_ASK_STAGE
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _gate_prompt, _presized

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "gate_return_resume_no_stage_reply_44e645.json").read_text(encoding="utf-8")
)


def _stages(prompt: str, gate: dict) -> bool:
    """Whether the prompt's stage-act condition (`its arm is X, or is Y with a resume_plan`) admits `gate`."""
    cond = re.search(r"halt is null and its arm is (\w+)(?:, or is (\w+) with a resume_plan)?", prompt)
    assert cond, "gate prompt carries no stage-act condition"
    if gate.get("halt") is not None:
        return False
    arm = gate["arm"].upper()
    return arm == cond.group(1) or bool(cond.group(2) and arm == cond.group(2) and gate.get("resume_plan"))


def test_the_44e645_resume_gate_shape_is_told_to_stage(monkeypatch):
    prompt = _gate_prompt(_presized(monkeypatch))
    assert OP_ASK_STAGE in prompt and _FIXTURE["arm"] == "s" and _FIXTURE["resume_plan"]
    assert _stages(prompt, _FIXTURE)


def test_an_s_gate_without_resume_plan_a_halted_gate_and_other_arms_are_not(monkeypatch):
    prompt = _gate_prompt(_presized(monkeypatch))
    assert not _stages(prompt, {**_FIXTURE, "resume_plan": None})
    assert not _stages(prompt, {**_FIXTURE, "halt": {"kind": "refusal"}})
    assert not _stages(prompt, {**_FIXTURE, "arm": "m"})
    assert _stages(prompt, {**_FIXTURE, "arm": "xs", "resume_plan": None})
