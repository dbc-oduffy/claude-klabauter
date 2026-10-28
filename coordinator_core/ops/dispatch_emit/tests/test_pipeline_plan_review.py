"""The plan-review pipeline: a claims-check stage then a domain-reviewer fix stage per plan."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest
from coordinator_core.ops.dispatch_emit.tests.pipeline_graph import agent_graph, subjects

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_doe_plan_review"
_PLANS = ["docs/plans/alpha.md", "docs/plans/beta.md"]
_REVIEWER = ["sid=coordinator:staff-eng"]

_DEFAULT_TYPE = "coordinator:staff-eng"


def _content(tmp_path: Path, *, reviewer_default: bool = False) -> Path:
    content = tmp_path / "content"
    target = content / "pipelines" / "plan-review"
    target.mkdir(parents=True)
    for source in _FIXTURE.iterdir():
        shutil.copy(source, target / source.name)
    if reviewer_default:
        manifest = target / "plan-review.manifest.yaml"
        text = manifest.read_text(encoding="utf-8")
        assert "      kind: roster\n" in text
        manifest.write_text(
            text.replace("      kind: roster\n", f'      kind: roster\n      default: ["staff-eng={_DEFAULT_TYPE}"]\n'),
            encoding="utf-8",
        )
    return content


def _emit(monkeypatch, tmp_path: Path, *, reviewer_default: bool = False, **params) -> str:
    content = _content(tmp_path, reviewer_default=reviewer_default)
    monkeypatch.setattr(op_module, "read_content_root", lambda: "doe")
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: content)
    work = tmp_path / "work"
    work.mkdir()
    out = work / "emitted.workflow.mjs"
    (work / "brief.md").write_text("review these plans", encoding="utf-8")
    _dispatch_emit({
        "pipeline": "plan-review", "brief": "brief.md", "target_root": str(work),
        "session_id": "sess-plan-review", "output_path": str(out),
        "subjects": _PLANS, "scratch_dir": "scratch/plan-review", **params,
    })
    return out.read_text(encoding="utf-8")


def test_fixture_declares_claims_check_then_fix():
    text = (_FIXTURE / "plan-review.manifest.yaml").read_text(encoding="utf-8")
    assert text.index("id: claims-check") < text.index("id: fix")
    assert "agent_type: coordinator:docs-checker" in text
    assert "agent_type_from: inputs.reviewer" in text
    assert "depends_on: [claims-check]" in text


def test_loader_accepts_the_manifest(tmp_path):
    manifest = load_manifest(_content(tmp_path), "plan-review")
    assert [s.id for s in manifest.stages] == ["claims-check", "fix"]
    assert manifest.lists == {"reviewer": "roster"}


def test_emitted_graph_is_claims_check_then_fix_per_plan(monkeypatch, tmp_path):
    script = _emit(monkeypatch, tmp_path, lists={"reviewer": _REVIEWER})
    assert subjects(script) == _PLANS
    graph = agent_graph(script)
    assert [g["agentType"] for g in graph] == ["coordinator:docs-checker", "coordinator:staff-eng"]
    assert not any(g["fanned"] for g in graph)


def test_each_plan_gets_its_own_claims_and_fix_output(monkeypatch, tmp_path):
    script = _emit(monkeypatch, tmp_path, lists={"reviewer": _REVIEWER})
    for plan in _PLANS:
        assert f"scratch/plan-review/{plan}-claims.md" in script
        assert f"scratch/plan-review/{plan}-fix.md" in script


def test_omitted_reviewer_takes_the_manifest_default(monkeypatch, tmp_path):
    script = _emit(monkeypatch, tmp_path, reviewer_default=True)
    assert [g["agentType"] for g in agent_graph(script)] == ["coordinator:docs-checker", _DEFAULT_TYPE]


def test_given_reviewer_overrides_the_default(monkeypatch, tmp_path):
    script = _emit(monkeypatch, tmp_path, reviewer_default=True, lists={"reviewer": ["ds=coordinator:staff-data-sci"]})
    assert [g["agentType"] for g in agent_graph(script)] == ["coordinator:docs-checker", "coordinator:staff-data-sci"]
    assert _DEFAULT_TYPE not in script


def test_two_type_reviewer_roster_is_refused_naming_the_fix_stage(monkeypatch, tmp_path):
    with pytest.raises(PipelineEmitRefused) as info:
        _emit(monkeypatch, tmp_path, lists={"reviewer": ["a=coordinator:staff-eng", "b=coordinator:staff-data-sci"]})
    assert any("'fix'" in r and "one agent type" in r for r in info.value.reasons)
