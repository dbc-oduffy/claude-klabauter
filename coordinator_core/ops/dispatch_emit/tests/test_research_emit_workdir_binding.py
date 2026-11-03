"""_pipeline_scratch_rel: a docs/research scratch binding must be a run dir's workdir."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused

RUN = "docs/research/topic-a/2026-11-03-some-run"


def test_default_binding_is_run_scratch(tmp_path):
    assert op_module._pipeline_scratch_rel(tmp_path, None, "run-1") == f"{op_module.RUN_DIR_ROOT}/run-1"


@pytest.mark.parametrize("scratch", [f"{RUN}/workdir", f"{RUN}/workdir/seg-1"])
def test_run_dir_workdir_accepted(tmp_path, scratch):
    assert op_module._pipeline_scratch_rel(tmp_path, scratch, "run-1") == scratch


@pytest.mark.parametrize(
    "scratch",
    [
        "docs/research/2026-11-03-some-run-workdir",
        "docs/research",
        "docs/research/topic-a",
        RUN,
        f"{RUN}/outputs",
        "docs/research/topic-a/not-a-run/workdir",
    ],
)
def test_docs_research_non_workdir_refused(tmp_path, scratch):
    with pytest.raises(PipelineEmitRefused, match="not a run dir's workdir"):
        op_module._pipeline_scratch_rel(tmp_path, scratch, "run-1")


def test_non_research_scratch_unchanged(tmp_path):
    assert op_module._pipeline_scratch_rel(tmp_path, "scratch/x", "run-1") == "scratch/x"
    assert op_module._pipeline_scratch_rel(tmp_path, "docs/plans/x", "run-1") == "docs/plans/x"
