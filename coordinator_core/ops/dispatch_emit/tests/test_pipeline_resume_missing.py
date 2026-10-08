"""dispatch.emit pipeline route, resume_missing: only the input-list elements without a landed output are re-emitted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused

_MANIFEST = """\
schema_version: 1
pipeline: batch-read
description: Readers fan out over batch ids, one result file each.
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
"""


@pytest.fixture
def repo(monkeypatch, tmp_path):
    content = tmp_path / "doe-content"
    pipe = content / "pipelines" / "batch-read"
    pipe.mkdir(parents=True)
    (pipe / "batch-read.manifest.yaml").write_text(_MANIFEST, encoding="utf-8")
    (pipe / "reader.md").write_text("Read batch {{item}} into {{stage.readers.output}}", encoding="utf-8")
    monkeypatch.setattr(op_module, "read_content_root", lambda: "doe")
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: content)
    root = tmp_path / "repo"
    (root / "scratch/run").mkdir(parents=True)
    (root / "brief.md").write_text("read", encoding="utf-8")
    return root


def _emit(root, **extra):
    return _dispatch_emit({
        "pipeline": "batch-read", "brief": "brief.md", "target_root": str(root),
        "session_id": "sess-1", "lists": {"batches": ["b1", "b2", "b3", "b4"]},
        "scratch_dir": "scratch/run", **extra,
    })


def _script(reply):
    return Path(reply["path"]).read_text(encoding="utf-8")


def test_only_batches_without_a_landed_result_are_re_emitted(repo):
    (repo / "scratch/run/batch-b1.json").write_text("{}", encoding="utf-8")
    (repo / "scratch/run/batch-b3.json").write_text("{}", encoding="utf-8")

    reply = _emit(repo, resume_missing=True)

    script = _script(reply)
    assert "Read batch b2" in script and "Read batch b4" in script
    assert "Read batch b1" not in script and "Read batch b3" not in script
    assert reply["resume_missing"] == {"batches": {"expected": 4, "present": 2, "missing": 2}}
    receipt = json.loads(Path(reply["receipt"]).read_text(encoding="utf-8"))
    assert receipt["resume_missing"] == reply["resume_missing"]


def test_an_empty_result_file_does_not_count_as_landed(repo):
    (repo / "scratch/run/batch-b1.json").write_text("", encoding="utf-8")
    (repo / "scratch/run/batch-b2.json").mkdir()

    reply = _emit(repo, resume_missing=True)

    assert reply["resume_missing"]["batches"]["present"] == 0
    assert "Read batch b1" in _script(reply) and "Read batch b2" in _script(reply)


def test_nothing_missing_is_refused_with_nothing_written(repo):
    for item in ("b1", "b2", "b3", "b4"):
        (repo / f"scratch/run/batch-{item}.json").write_text("{}", encoding="utf-8")

    with pytest.raises(PipelineEmitRefused, match="nothing to re-emit"):
        _emit(repo, resume_missing=True)
    assert not (repo / "scratch/warp").exists()


def test_without_the_flag_every_batch_is_emitted(repo):
    (repo / "scratch/run/batch-b1.json").write_text("{}", encoding="utf-8")

    reply = _emit(repo)

    assert "Read batch b1" in _script(reply)
    assert "resume_missing" not in reply


def test_resume_needs_the_interrupted_runs_scratch_dir(repo):
    with pytest.raises(ValueError, match="scratch_dir"):
        _dispatch_emit({
            "pipeline": "batch-read", "brief": "brief.md", "target_root": str(repo),
            "lists": {"batches": ["b1"]}, "resume_missing": True,
        })


def test_resume_flag_must_be_boolean_and_pipeline_only(repo):
    with pytest.raises(ValueError, match="boolean"):
        _emit(repo, resume_missing="yes")
    with pytest.raises(ValueError, match="pipeline"):
        _dispatch_emit({"plan_path": "p.md", "resume_missing": True})


def test_pipeline_emit_declares_the_run_scratch_dir_as_a_reader_output_root(repo, monkeypatch, tmp_path):
    from coordinator_core.bash_guards import _write_bump_applicability as applicability

    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))

    reply = _emit(repo)

    assert reply["run_output_root"] == (repo / "scratch/run").resolve().as_posix()
    declaration = json.loads(
        (applicability.run_output_roots_dir() / f"{reply['run_id']}.json").read_text(encoding="utf-8")
    )
    assert Path(declaration["root"]) == Path(reply["run_output_root"])
    assert applicability.target_is_under_declared_run_output_root(str(repo / "scratch/run/batch-b1.json"))
    assert not applicability.target_is_under_declared_run_output_root(str(repo / "brief.md"))
