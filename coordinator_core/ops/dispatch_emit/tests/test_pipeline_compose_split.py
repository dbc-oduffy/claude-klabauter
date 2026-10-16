"""compose_chain_parts: one script when the chain fits the cap, one per pipeline when it does not."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import pipeline_compose
from coordinator_core.ops.dispatch_emit.pipeline_compose import (
    ChainPart,
    compose_chain_parts,
    compose_chain_script,
)
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    FanOut,
    Manifest,
    PipelineEmitRefused,
    PipelineInputs,
    Schedule,
    Stage,
    WORKFLOW_SCRIPT_CAP_BYTES,
)

KW = {"run_id": "r", "agent_type_host": None}


def _segment(pipeline, template="Write under {{scratch_dir}} for {{brief}}. " + "pad " * 50):
    stage = Stage(
        id="w", agent_type="coordinator:b", model="sonnet", template="t.md", schema=None,
        web_caller=False, depends_on=(), fan_out=FanOut("over", None, None, inputs="things"),
        output="{{scratch_dir}}/{{item}}",
    )
    manifest = Manifest(
        pipeline=pipeline, root=Path("m"), flags={}, stages=(stage,), templates={"t.md": template},
        schemas={}, sha256="0" * 64, lists={"things": "list"},
    )
    inputs = PipelineInputs("b", (), "state/scratch/y", {}, lists={"things": ("a",)})
    schedule = Schedule(scope={"w": "post"}, levels={"post": (("w",),)})
    return manifest, inputs, schedule


def test_under_cap_chain_is_one_part_equal_to_chain_script():
    segments = [_segment("alpha"), _segment("beta")]
    parts = compose_chain_parts(segments, **KW)
    assert parts == (ChainPart(("alpha", "beta"), compose_chain_script(segments, **KW)),)


def test_over_cap_chain_splits_one_part_per_pipeline(monkeypatch):
    segments = [_segment("alpha"), _segment("beta")]
    whole = len(compose_chain_script(segments, **KW).encode("utf-8"))
    monkeypatch.setattr(pipeline_compose, "WORKFLOW_SCRIPT_CAP_BYTES", whole - 1)
    action = {"op": "research.close"}
    parts = compose_chain_parts(segments, next_action=action, **KW)
    assert [p.pipelines for p in parts] == [("alpha",), ("beta",)]
    assert "state/scratch/y/beta" not in parts[0].script
    assert "state/scratch/y/beta" in parts[1].script
    assert "next_action" not in parts[0].script
    assert "next_action" in parts[1].script


def test_over_cap_split_groups_a_shared_leg_into_one_part(monkeypatch):
    segments = [_segment("pre"), _segment("nb"), _segment("web")]
    legs = {"pre": "nb"}
    whole = len(compose_chain_script(segments, legs=legs, **KW).encode("utf-8"))
    monkeypatch.setattr(pipeline_compose, "WORKFLOW_SCRIPT_CAP_BYTES", whole - 1)
    parts = compose_chain_parts(segments, next_action={"op": "research.close"}, legs=legs, **KW)
    assert [p.pipelines for p in parts] == [("pre", "nb"), ("web",)]
    assert "state/scratch/y/nb" in parts[0].script and "state/scratch/y/web" in parts[1].script
    assert "state/scratch/y/nb/nb" not in parts[0].script
    assert "break segment2" in parts[0].script and "segment_halts" in parts[1].script
    assert "next_action" not in parts[0].script and "next_action" in parts[1].script


def test_lone_over_cap_segment_still_raises_naming_the_cap():
    big = _segment("alpha", template="x" * (WORKFLOW_SCRIPT_CAP_BYTES + 10))
    with pytest.raises(PipelineEmitRefused) as err:
        compose_chain_parts([big], **KW)
    assert str(WORKFLOW_SCRIPT_CAP_BYTES) in str(err.value)


def test_split_part_still_over_cap_raises(monkeypatch):
    segments = [_segment("alpha"), _segment("beta")]
    monkeypatch.setattr(pipeline_compose, "WORKFLOW_SCRIPT_CAP_BYTES", 100)
    with pytest.raises(PipelineEmitRefused, match="over the Workflow cap"):
        compose_chain_parts(segments, **KW)
