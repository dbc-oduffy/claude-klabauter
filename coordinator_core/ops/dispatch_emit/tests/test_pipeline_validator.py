"""dispatch.emit pipeline route, validator: a stage naming {{validator}} runs per batch, only when a validator is given."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused

_MANIFEST = """\
schema_version: 1
pipeline: batch-read
description: Readers fan out over batch ids; a validator checks each batch's result.
inputs:
  brief: required
  scratch_dir: optional
  lists:
    batches:
      kind: strings
stages:
  - id: readers
    phase: Read
    agent_type: coordinator:research-specialist
    model: haiku
    template: reader.md
    fan_out:
      over: inputs.batches
    output: "{{scratch_dir}}/batch-{{item}}.json"
  - id: validate
    phase: Validate
    agent_type: coordinator:research-specialist
    model: haiku
    template: validate.md
    depends_on: [readers]
    fan_out:
      over: inputs.batches
    output: "{{scratch_dir}}/valid-{{item}}.txt"
"""

_BARE_MANIFEST = _MANIFEST.split("  - id: validate")[0]


def _repo(monkeypatch, tmp_path, manifest):
    content = tmp_path / "doe-content"
    pipe = content / "pipelines" / "batch-read"
    pipe.mkdir(parents=True)
    (pipe / "batch-read.manifest.yaml").write_text(manifest, encoding="utf-8")
    (pipe / "reader.md").write_text("Read batch {{item}} into {{scratch_dir}}", encoding="utf-8")
    (pipe / "validate.md").write_text("Run `{{validator}}` over batch {{item}}", encoding="utf-8")
    monkeypatch.setattr(op_module, "read_content_root", lambda: "doe")
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: content)
    root = tmp_path / "repo"
    (root / "scratch/run").mkdir(parents=True)
    (root / "brief.md").write_text("read", encoding="utf-8")
    return root


@pytest.fixture
def repo(monkeypatch, tmp_path):
    return _repo(monkeypatch, tmp_path, _MANIFEST)


def _emit(root, **extra):
    return _dispatch_emit({
        "pipeline": "batch-read", "brief": "brief.md", "target_root": str(root),
        "session_id": "sess-1", "lists": {"batches": ["b1", "b2"]},
        "scratch_dir": "scratch/run", **extra,
    })


def _script(reply):
    return Path(reply["path"]).read_text(encoding="utf-8")


def test_validator_runs_once_per_batch_after_the_readers(repo):
    reply = _emit(repo, validator="python3 check.py --strict")

    script = _script(reply)
    assert script.count("Run `python3 check.py --strict` over batch b1") == 1
    assert script.count("Run `python3 check.py --strict` over batch b2") == 1
    assert script.index("Read batch b2") < script.index("Run `python3 check.py --strict` over batch b1")
    receipt = json.loads(Path(reply["receipt"]).read_text(encoding="utf-8"))
    assert receipt["validator"] == "python3 check.py --strict"


def test_without_a_validator_the_validate_stage_is_not_emitted(repo):
    reply = _emit(repo)

    script = _script(reply)
    assert "Read batch b1" in script
    assert "phases: ['Read']" in script and "Run `" not in script
    assert "validator" not in json.loads(Path(reply["receipt"]).read_text(encoding="utf-8"))


def test_a_validator_no_stage_names_is_refused(monkeypatch, tmp_path):
    root = _repo(monkeypatch, tmp_path, _BARE_MANIFEST)

    with pytest.raises(PipelineEmitRefused, match="no stage of 'batch-read' names"):
        _emit(root, validator="python3 check.py")


@pytest.mark.parametrize("bad", ["", "   ", "a\nb"])
def test_a_blank_or_multiline_validator_is_refused(repo, bad):
    with pytest.raises(PipelineEmitRefused, match="single-line command"):
        _emit(repo, validator=bad)


def test_validator_must_be_a_string(repo):
    reply = _emit(repo, validator=["python3", "check.py"])

    assert reply == {"error": "dispatch.emit params.validator must be a string"}


def test_resume_missing_re_emits_the_validator_for_a_batch_without_its_verdict(repo):
    (repo / "scratch/run/batch-b1.json").write_text("{}", encoding="utf-8")
    (repo / "scratch/run/batch-b2.json").write_text("{}", encoding="utf-8")
    (repo / "scratch/run/valid-b1.txt").write_text("ok", encoding="utf-8")

    script = _script(_emit(repo, validator="v.py", resume_missing=True))

    assert "over batch b2" in script and "over batch b1" not in script


def test_cli_validator_flag_requires_the_pipeline_route(capsys):
    code = cli_module.main(["--validator", "v.py"])

    assert code == cli_module.EXIT_USAGE
    assert "--validator" in capsys.readouterr().err
