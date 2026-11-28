"""Acceptance: an EM-chosen `stage_families` map names scout/research/review runners; no choice is Claude."""
from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import stage_family, stage_family_binding

_CHOSEN = {"scout": "grok", "research": "agy", "review": "codex"}


def _sizing_from_disk(tmp_path, body: dict) -> dict:
    path = tmp_path / "sizing.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8", newline="\n")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_chosen_roles_each_build_a_runner_brief_for_their_family(tmp_path) -> None:
    sizing = _sizing_from_disk(tmp_path, {"intent": "Route stages", "stage_families": _CHOSEN})
    binding = stage_family_binding.from_sizing(sizing)
    assert binding.runner == stage_family.RUNNER_AGENT
    for role, family in _CHOSEN.items():
        assert binding.roles[role] == family
        brief = stage_family.runner_brief(family, role, "plan.md", "scratch/x")
        assert f"family: {family}\n" in brief and f"stage: {role}\n" in brief


def test_sizing_without_a_choice_routes_every_role_to_claude(tmp_path) -> None:
    sizing = _sizing_from_disk(tmp_path, {"intent": "Route stages"})
    assert stage_family_binding.from_sizing(sizing) is None


def test_sizing_carrying_the_retired_key_is_refused(tmp_path) -> None:
    sizing = _sizing_from_disk(tmp_path, {"stage_family_binding": "coordinator-content-repo:x.yaml"})
    with pytest.raises(stage_family_binding.BindingError, match="stage_families"):
        stage_family_binding.from_sizing(sizing)


def test_failed_agy_research_attempt_is_recorded_then_the_claude_fallback() -> None:
    agy = {
        "family": "agy", "stage": "research", "exit_code": 1, "output_path": "scratch/r.md",
        "verdict": "FAILED", "blocked_reason": "agy: out of credits",
    }
    claude = {"family": "claude", "stage": "research", "exit_code": 0, "output_path": "scratch/c.md", "verdict": "OK"}
    rec = stage_family.stage_record("research", agy, claude)
    assert [a["family"] for a in rec["attempts"]] == ["agy", "claude"]
    assert rec["attempts"][0]["verdict"] == "FAILED"
    assert rec["attempts"][0]["blocked_reason"] == "agy: out of credits"
    assert "fallback_for" not in rec["attempts"][0]
    assert rec["attempts"][1]["fallback_for"] == "agy"
    assert rec["reported_family"] == "claude"


def test_failed_family_without_a_blocked_reason_is_refused() -> None:
    agy = {"family": "agy", "stage": "research", "exit_code": 1, "output_path": "", "verdict": "FAILED"}
    claude = {"family": "claude", "stage": "research", "exit_code": 0, "output_path": "", "verdict": "OK"}
    with pytest.raises(ValueError):
        stage_family.stage_record("research", agy, claude)
