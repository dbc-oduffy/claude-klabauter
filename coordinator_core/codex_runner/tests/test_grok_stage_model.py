"""Workflow tiers select Grok catalogue ids and pass reasoning effort through."""
from pathlib import Path

import pytest

from coordinator_core.agent_pipe.model_tiers import model_for_tier
from coordinator_core.codex_runner import workflow


def test_sonnet_stage_selects_the_execute_model_and_keeps_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "resolve_grok", lambda: ["/bin/grok-x"])
    argv = workflow._family_argv(
        "grok", tmp_path, model=workflow._grok_model({"model": "sonnet"}),
        effort=workflow._grok_effort({"effort": "low"}),
    )
    assert argv[argv.index("--model") + 1] == model_for_tier("grok", "sonnet")
    assert argv[argv.index("--reasoning-effort") + 1] == "low"
    assert "--no-subagents" in argv


def test_opus_stage_selects_the_plan_model(monkeypatch: pytest.MonkeyPatch) -> None:
    assert workflow._grok_model({"model": "opus"}) == model_for_tier("grok", "opus")
    assert workflow._grok_model({}) is None
    assert workflow._grok_effort({}) is None
    assert workflow._grok_effort({"model": "opus"}) is None
    assert workflow._grok_effort({"model": "sonnet"}) is None
    assert workflow._grok_effort({"model": "haiku"}) is None
    assert workflow._grok_effort({"grokModel": "grok-4.6"}) is None
    assert workflow._grok_effort({"model": "sonnet", "effort": "xhigh"}) == "xhigh"


def test_unmapped_tier_and_unknown_effort_fail() -> None:
    with pytest.raises(workflow.AgentFailed, match="no verified Grok model"):
        workflow._grok_model({"model": "fable"})
    with pytest.raises(workflow.AgentFailed, match="reasoning effort"):
        workflow._grok_effort({"effort": "ultra"})


def test_explicit_grok_model_wins() -> None:
    assert workflow._grok_model({"grokModel": "grok-4.6", "model": "sonnet"}) == "grok-4.6"
