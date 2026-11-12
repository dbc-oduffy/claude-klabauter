"""Acceptance: an opted-in research run routes scout to grok and records agy FAILED then claude for specialists."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
import coordinator_core.sizing_assemble as sa
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit import stage_family, stage_family_binding
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_parts
from coordinator_core.ops.emit import doe_drift
from coordinator_core.session import record_homes

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_research"
_NAME = "2026-11-12-stage-family-research.yaml"
_DRAFT = """schema: sizing-object
intent: "Research the thing"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""
_BINDING = """schema_version: 1
runner: coordinator:family-stage-runner
fallback: claude
roles:
  scout: grok
  research: agy
  review: codex
"""
_WEB_MANIFEST = """schema_version: 1
pipeline: web
description: Fixture web pipeline with a scout stage and a specialists stage.
inputs:
  brief: required
  scratch_dir: optional
stages:
  - id: scout
    phase: Scout
    agent_type: general-purpose
    model: sonnet
    template: web-scope-template.md
    fan_out: none
    output: "{{scratch_dir}}/scout.md"
  - id: specialists
    phase: Specialists
    agent_type: general-purpose
    model: sonnet
    template: web-sweep-template.md
    fan_out: none
    depends_on: [scout]
    output: "{{scratch_dir}}/sweep.md"
"""
_FLAGS = ("--research-class", "corpus", "--research-appetite", "medium", "--research-source", "web")


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


@pytest.fixture
def repo(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    content = tmp_path / "content"
    shutil.copytree(_FIXTURE, content)
    (content / "pipelines" / "deep-research" / "web.manifest.yaml").write_text(
        _WEB_MANIFEST, encoding="utf-8", newline="\n"
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: content)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-research-family")
    return root


@pytest.fixture
def content_root(tmp_path, monkeypatch):
    root = tmp_path / "doe"
    (root / "coordinator" / "pipelines").mkdir(parents=True)
    (root / "coordinator" / "pipelines" / "stage-family-binding.yaml").write_text(
        _BINDING, encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr(doe_drift, "resolve_doe_clone", lambda: root)
    return root


def _write_sizing(repo: Path, binding: str | None) -> str:
    home = Path(record_homes.home_dir(str(repo), "sizings"))
    home.mkdir(parents=True, exist_ok=True)
    (home / _NAME).write_text(_DRAFT, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    assert sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--write", rel, *_FLAGS]) == sa.EXIT_OK
    if binding is not None:
        path = repo / rel
        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n") + f"\nstage_family_binding: {binding}\n",
            encoding="utf-8", newline="\n",
        )
    return rel


def _emit(capsys, rel: str):
    capsys.readouterr()
    code = cli_module.main(["--from-sizing", rel])
    out = capsys.readouterr()
    return code, out


def _script(repo: Path, binding: str | None, capsys) -> tuple[str, dict]:
    rel = _write_sizing(repo, binding)
    code, out = _emit(capsys, rel)
    assert code == cli_module.EXIT_OK, out.err
    reply = json.loads(out.out)
    return Path(reply["path"]).read_text(encoding="utf-8"), reply


def test_opted_in_scout_stage_runs_through_the_runner_on_grok(repo, content_root, capsys):
    script, reply = _script(repo, stage_family_binding.BINDING_REF, capsys)
    assert f"agentType: '{stage_family.RUNNER_AGENT}'" in script
    assert "async function _familyStage(family, role, prompt, label, opts)" in script
    assert "_familyStage('grok', 'scout'," in script
    assert "family: grok\\n" in script and "stage: scout\\n" in script
    receipt = json.loads(Path(reply["receipt"]).read_text(encoding="utf-8"))
    assert receipt["stage_families"] == {"web.scout": "grok", "web.specialists": "agy"}


def test_opted_in_specialists_stage_records_agy_failed_then_the_claude_fallback(repo, content_root, capsys):
    script, _ = _script(repo, stage_family_binding.BINDING_REF, capsys)
    assert "_familyStage('agy', 'research'," in script
    assert "family: agy\\n" in script and "stage: research\\n" in script
    arm = script[script.index("async function _familyStage("):]
    arm = arm[: arm.index("\n}\n") + 3]
    assert "verdict: 'FAILED'" in arm
    assert "attempts: [fam, { ...fb, fallback_for: fam.family }]" in arm
    assert "reported_family: 'claude'" in arm
    assert ":fallback" in arm
    for key in stage_family._RESULT_KEYS:
        assert key in script
    record = stage_family.stage_record(
        "research",
        {"family": "agy", "stage": "research", "exit_code": 1, "output_path": "", "verdict": "FAILED", "blocked_reason": "x"},
        {"family": "claude", "stage": "research", "exit_code": 0, "output_path": "", "verdict": "OK"},
    )
    assert set(record) == {"stage_role", "attempts", "reported_family"}
    assert record["attempts"][1]["fallback_for"] == "agy"


def test_a_sizing_without_the_binding_emits_the_unmapped_compose_unchanged(repo, monkeypatch, capsys):
    monkeypatch.setattr(
        stage_family_binding, "load", lambda *a, **k: pytest.fail("binding read without opt-in")
    )
    script, _ = _script(repo, None, capsys)
    assert "family-stage-runner" not in script and "_familyStage" not in script
    rel = _write_sizing(repo, None)
    ctx, _, _ = op_module._research_route_setup({}, repo, rel, None)
    assert ctx["stage_family_opted_in"] is False
    baseline = compose_chain_parts(
        ctx["segments"], run_id=ctx["run_id"], agent_type_host=None,
        next_action=ctx["next_action"], legs=ctx["legs"],
    )[0].script
    plain = compose_chain_parts(
        ctx["segments"], run_id=ctx["run_id"], agent_type_host=None,
        next_action=ctx["next_action"], legs=ctx["legs"], stage_families=None,
    )[0].script
    assert baseline == plain
    assert "family-stage-runner" not in baseline


def test_an_unknown_binding_ref_refuses_the_emit(repo, content_root, capsys):
    rel = _write_sizing(repo, "coordinator-content-repo:somewhere/else.yaml")
    code, out = _emit(capsys, rel)
    assert code != cli_module.EXIT_OK
    assert "is not the binding" in out.err


def test_an_unresolvable_binding_refuses_with_its_message_not_a_silent_claude(repo, monkeypatch, capsys):
    def gone():
        raise doe_drift.DoeResolveError("no clone")

    monkeypatch.setattr(doe_drift, "resolve_doe_clone", gone)
    rel = _write_sizing(repo, stage_family_binding.BINDING_REF)
    code, out = _emit(capsys, rel)
    assert code != cli_module.EXIT_OK
    assert "cannot resolve the DoE root" in out.err
