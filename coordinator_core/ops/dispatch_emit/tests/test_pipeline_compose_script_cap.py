"""Composer: the script cap refuses an oversize script, and string-item prompts ride once."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import pipeline_compose
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_pipeline_script
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    FanOut,
    Manifest,
    PipelineEmitRefused,
    PipelineInputs,
    Schedule,
    Stage,
    WORKFLOW_SCRIPT_CAP_BYTES,
)

TEMPLATE = "Work on {{item}} with {{brief}}. " + "pad " * 50


def _parts(items, template=TEMPLATE):
    stage = Stage(
        id="w", agent_type="coordinator:b", model="sonnet", template="t.md", schema=None,
        web_caller=False, depends_on=(), fan_out=FanOut("over", None, None, inputs="things"),
        output="{{scratch_dir}}/{{item}}",
    )
    manifest = Manifest(
        pipeline="p", root=Path("m"), flags={}, stages=(stage,), templates={"t.md": template},
        schemas={}, sha256="0" * 64, lists={"things": "list"},
    )
    inputs = PipelineInputs("b", (), "state/scratch/y", {}, lists={"things": tuple(items)})
    schedule = Schedule(scope={"w": "post"}, levels={"post": (("w",),)})
    return manifest, inputs, schedule


def _compose(parts):
    return compose_pipeline_script(*parts, run_id="r", agent_type_host=None)


def test_string_item_prompts_share_one_template_copy():
    script = _compose(_parts(["alpha", "beta", "gamma", "delta"]))
    assert script.count("Work on ") == 1
    assert "alpha" in script and "delta" in script
    assert "withItem(common.prompts['w'], item.slug)" in script


def test_over_cap_script_is_refused_naming_size_cap_and_largest_segment():
    parts = _parts(["a"], template="x" * (WORKFLOW_SCRIPT_CAP_BYTES + 10))
    with pytest.raises(PipelineEmitRefused) as err:
        _compose(parts)
    (reason,) = err.value.reasons
    assert str(WORKFLOW_SCRIPT_CAP_BYTES) in reason and "largest segment: p" in reason
    assert "bytes, over the Workflow cap" in reason


def test_cap_is_measured_on_utf8_bytes_not_characters(monkeypatch):
    parts = _parts(["a"])
    base = len(_compose(parts).encode("utf-8"))
    monkeypatch.setattr(pipeline_compose, "WORKFLOW_SCRIPT_CAP_BYTES", base)
    assert _compose(parts)
    monkeypatch.setattr(pipeline_compose, "WORKFLOW_SCRIPT_CAP_BYTES", base - 1)
    with pytest.raises(PipelineEmitRefused):
        _compose(parts)
