"""The pre-sized gate leg stages an S-arm resume in-process: the 44e645 gate return stages, a plain S does not."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import OP_ASK_STAGE
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _gate_prompt, _presized
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_gate_leg_carries_bash import _closing

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "gate_return_resume_no_stage_reply_44e645.json").read_text(encoding="utf-8")
)
_ACT = {"stage_act": True, "run_id": "r1", "cross_repo_approved": False, "gated": []}


def _staged(repo, monkeypatch: pytest.MonkeyPatch, gate: dict) -> list:
    calls: list = []
    monkeypatch.setattr(ask_stage, "_handler", lambda p, r=None: calls.append(p) or {"manifest_handle": {"path": "m"}})
    _closing(repo, _ACT, dict(gate))
    return calls


def test_the_gate_prompt_hands_staging_to_the_op_not_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    prompt = _gate_prompt(_presized(monkeypatch))
    assert "stage_act: true" in prompt and OP_ASK_STAGE not in prompt


def test_the_44e645_resume_gate_shape_is_staged(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _FIXTURE["arm"] == "s" and _FIXTURE["resume_plan"]
    calls = _staged(repo, monkeypatch, _FIXTURE)
    assert len(calls) == 1
    assert calls[0]["plan_path"] == _FIXTURE["resume_plan"] and calls[0]["only_rows"] == _FIXTURE["resume_rows"]
    assert calls[0]["commit_sizing_path"] == REL and "writes" not in calls[0] and "sizing_path" not in calls[0]


def test_an_s_gate_without_resume_plan_a_halted_gate_and_other_arms_are_not(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not _staged(repo, monkeypatch, {**_FIXTURE, "resume_plan": None})
    assert not _staged(repo, monkeypatch, {**_FIXTURE, "halt": {"kind": "refusal"}})
    assert not _staged(repo, monkeypatch, {**_FIXTURE, "arm": "m_plus"})
    assert _staged(repo, monkeypatch, {**_FIXTURE, "arm": "xs", "resume_plan": None})
