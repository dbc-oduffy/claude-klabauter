"""segment_name: how a keyed segment is named."""

from pathlib import Path

from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    Manifest, PipelineInputs, segment_name,
)


def _manifest() -> Manifest:
    return Manifest(
        pipeline="repo", root=Path("."), flags={}, stages=(), templates={}, schemas={}, sha256="x",
    )


def _inputs(**kw) -> PipelineInputs:
    return PipelineInputs(brief="b.md", subjects=(), scratch_dir="s", flags={}, **kw)


def test_segment_name_is_the_key_when_set():
    assert segment_name(_manifest(), _inputs(segment_key="repo-1-engine")) == "repo-1-engine"


def test_segment_name_is_the_pipeline_when_unkeyed():
    assert segment_name(_manifest(), _inputs()) == "repo"


def test_unkeyed_inputs_equal_explicit_none():
    assert _inputs() == _inputs(segment_key=None)
    assert _inputs().segment_key is None
