"""Acceptance: an opted-in run routes scout/review from the binding and records agy FAILED then claude."""
from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, stage_family, stage_family_binding
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW, write_run_scripts
from coordinator_core.ops.emit import doe_drift

PLAN_REL = "docs/plans/2026-11-12-binding-acceptance.md"
SIZING_REL = "state/sizings/2026-11-12-binding-acceptance.yaml"
_FAMILY_FIELDS = ("family", "stage_role", "fallback_brief_path", "fallback_agent_type")

_BINDING = """schema_version: 1
runner: coordinator:family-stage-runner
fallback: claude
roles:
  scout: grok
  research: agy
  review: codex
"""

_PLAN = """---
deliverable_id: dlv-binding-abc123
---
# Binding acceptance

## Problem

Rows with scout, research and review roles.

## Tasks

```yaml plan-tasks
- id: C1
  title: scout
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
  stage_role: scout
- id: C2
  title: research
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
  stage_role: research
- id: C3
  title: review
  change_kind: code-edit
  surface: pkg/c.py
  writes:
    - pkg/c.py
  stage_role: review
```
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    write_run_scripts(tmp_path, "r1", "x1run")
    return tmp_path


@pytest.fixture
def content_root(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("doe")
    (root / "coordinator" / "pipelines").mkdir(parents=True)
    (root / "coordinator" / "pipelines" / "stage-family-binding.yaml").write_text(
        _BINDING, encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(doe_drift, "resolve_doe_clone", lambda: root)
    return root


def _stage(repo, opted_in: bool):
    (repo / PLAN_REL).write_text(_PLAN, encoding="utf-8", newline="\n")
    sizing = {
        "intent": "Route stages",
        "exit_criterion": {"statement": "it works"},
        "deliverable_id": "dlv-xs-abc123",
    }
    if opted_in:
        sizing["stage_family_binding"] = stage_family_binding.BINDING_REF
    (repo / SIZING_REL).write_text(yaml.safe_dump(sizing), encoding="utf-8", newline="\n")
    return stage(repo, run_id="r1", plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)


def _script(monkeypatch, opted_in: bool) -> str:
    sizing = {"stage_family_binding": stage_family_binding.BINDING_REF} if opted_in else {}
    monkeypatch.setattr(ask_compose, "load_sizing", lambda root, rel: sizing)
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="sizing.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", **REVIEW_KW,
    )


def test_opted_in_run_dispatches_scout_to_grok_review_to_codex_research_to_agy(repo, content_root):
    by_id = {r.id: r for r in _stage(repo, opted_in=True).rows}
    for rid, family, role in (("C1", "grok", "scout"), ("C2", "agy", "research"), ("C3", "codex", "review")):
        row = by_id[rid]
        assert row.agent_type == stage_family.RUNNER_AGENT
        assert (row.family, row.stage_role) == (family, role)
        brief = (repo / row.brief_path).read_text(encoding="utf-8")
        assert f"family: {family}\n" in brief and f"stage: {role}\n" in brief
        assert (repo / row.fallback_brief_path).is_file()
        assert row.fallback_agent_type == "coordinator:executor"


def test_failed_agy_research_attempt_is_recorded_then_the_claude_fallback():
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


def test_failed_family_without_a_blocked_reason_is_refused():
    agy = {"family": "agy", "stage": "research", "exit_code": 1, "output_path": "", "verdict": "FAILED"}
    claude = {"family": "claude", "stage": "research", "exit_code": 0, "output_path": "", "verdict": "OK"}
    with pytest.raises(ValueError):
        stage_family.stage_record("research", agy, claude)


def test_opted_in_emitted_script_carries_the_fallback_arm(monkeypatch):
    script = _script(monkeypatch, opted_in=True)
    assert "r.family ? _familyRun(r) : " in script
    assert "async function _familyRun(r)" in script
    assert "fallback_for: _fam.family" in script


def test_non_opted_in_run_routes_unchanged(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("binding read without opt-in")

    monkeypatch.setattr(stage_family_binding, "load", boom)
    rows = _stage(repo, opted_in=False).rows
    assert {r.agent_type for r in rows} == {"coordinator:executor"}
    for r in rows:
        blob = json.dumps(r.to_json())
        assert not any(f'"{k}"' in blob for k in _FAMILY_FIELDS)
        assert "family:" not in (repo / r.brief_path).read_text(encoding="utf-8")
        assert not (repo / r.brief_path).with_name(f"{r.id}.fallback.md").exists()


def test_non_opted_in_emitted_script_carries_no_family_arm(monkeypatch):
    script = _script(monkeypatch, opted_in=False)
    assert "_familyRun" not in script and "_FAMILY_RESULT_SCHEMA" not in script
