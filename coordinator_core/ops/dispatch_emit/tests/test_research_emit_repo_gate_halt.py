"""research_emit.gate_repo: the repo pipeline's gate stage halts the run, naming the gate and the reason."""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _Admitted, _write_sizing
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import _stage

_FIXTURES = Path(__file__).parent / "fixtures"
_GATE_SCHEMA = {
    "type": "object",
    "required": ["failed"],
    "properties": {"failed": {"type": "array", "items": {"type": "string"}}},
}


def _stages() -> list[dict]:
    return [
        _stage("scout"),
        _stage("gate", depends_on=["scout"], schema=_GATE_SCHEMA),
        _stage("rescout", depends_on=["gate"]),
        _stage("synthesize", depends_on=["rescout"]),
    ]


def _repo_manifest(tmp_path: Path, stages: list[dict]):
    from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineInputs

    root = tmp_path / "content" / "pipelines" / "dr"
    root.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": 1, "pipeline": "repo", "inputs": {"brief": "required"}, "stages": stages}
    (root / "repo.manifest.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    (root / "t.md").write_text("work from {{brief}} in {{scratch_dir}}", encoding="utf-8")
    manifest = load_manifest(tmp_path / "content", "repo")
    inputs = PipelineInputs(brief="state/ask.md", subjects=(), scratch_dir="scratch/run", flags={})
    return manifest, inputs


def _gate(manifest):
    return next(s for s in manifest.stages if s.id == "gate")


def test_gate_stage_gets_a_halt_and_validates(tmp_path):
    manifest, inputs = _repo_manifest(tmp_path, _stages())
    gated = re_.gate_repo(manifest)
    assert _gate(manifest).halts_unless is None
    assert _gate(gated).halts_unless == "passed"
    schema = gated.schemas[_gate(gated).schema]
    assert set(schema["required"]) == {"failed", "passed", "remedy"}
    assert schema["properties"]["failed"] == _GATE_SCHEMA["properties"]["failed"]
    validate(gated, inputs)


def test_failed_gate_halts_before_every_later_stage_naming_gate_and_reason(tmp_path):
    manifest, inputs = _repo_manifest(tmp_path, _stages())
    gated = re_.gate_repo(manifest)
    script = compose_chain_script([(gated, inputs, validate(gated, inputs))], run_id="r1", agent_type_host=None)
    check = script.index("['gate']['passed'] === false")
    ret = script.index("decision_required: ", check)
    assert "halted: 'repo.gate'" in script[check:ret + 80]
    assert script.index("label: 'rescout'") > ret and script.index("label: 'synthesize'") > ret
    assert "naming the gate stage and the failed chunk letters" in str(gated.schemas[_gate(gated).schema])


def test_manifest_without_a_gate_stage_is_unchanged(tmp_path):
    manifest, _ = _repo_manifest(tmp_path, [_stage("scout"), _stage("synthesize", depends_on=["scout"])])
    assert re_.gate_repo(manifest) == manifest


def test_gate_that_already_halts_keeps_its_own_halt(tmp_path):
    schema = {
        "type": "object",
        "required": ["ok", "remedy", "failed"],
        "properties": {
            "ok": {"type": "boolean"}, "remedy": {"type": "array", "items": {"type": "string"}},
            "failed": {"type": "array", "items": {"type": "string"}},
        },
    }
    manifest, _ = _repo_manifest(tmp_path, [_stage("gate", halts_unless="ok", schema=schema)])
    assert re_.gate_repo(manifest) == manifest


def test_gate_without_an_inline_schema_is_unchanged(tmp_path):
    manifest, _ = _repo_manifest(tmp_path, [_stage("scout"), _stage("gate", depends_on=["scout"])])
    assert re_.gate_repo(manifest) == manifest


def test_default_research_emit_gates_the_repo_pipeline(monkeypatch, tmp_path):
    root = tmp_path / "content"
    shutil.copytree(_FIXTURES / "pipeline_research_multi_repo", root, dirs_exist_ok=True)
    path = root / "pipelines" / "deep-research" / "repo.manifest.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    gate = {
        "id": "gate", "phase": "Scouts", "agent_type": "general-purpose", "model": "sonnet",
        "template": "repo-template.md", "depends_on": ["scout-haiku", "scout-sonnet"],
        "schema": _GATE_SCHEMA, "output": "{{scratch_dir}}/gate.json",
    }
    doc["stages"].insert(3, gate)
    doc["stages"][4]["depends_on"] = ["gate"]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-gate-halt")
    flags = [a for n in range(1, 6) for a in ("--research-question", f"Question {n}?")]
    rel = _write_sizing(work, "--research-class", "corpus", "--research-source", "repo", *flags)
    ctx, _, _ = op_module._research_route_setup({}, work, rel, None)
    manifest = next(m for m, _i, _v in ctx["segments"] if m.pipeline == "repo")
    assert next(s for s in manifest.stages if s.id == "gate").halts_unless == "passed"
