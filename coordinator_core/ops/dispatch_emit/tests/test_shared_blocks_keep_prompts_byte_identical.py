
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from ._shared_expand import expand_shared

_WORKFLOW_SCRIPT_CAP = 524288


def _waves(count: int) -> list[list[WaveRow]]:
    return [
        [
            WaveRow(
                id=f"C{i}",
                title=f"chunk {i}",
                surface="pkg",
                writes=[f"pkg/mod{i}.py"],
                reads=[],
                depends_on=[],
            )
        ]
        for i in range(count)
    ]


def _compose(waves, *, shared: bool) -> str:
    context = emit.PlanContext(
        title="Example",
        goal="Goal text.",
        exit_criterion="Exit text.",
        problem_excerpt="Problem text.",
        repo_root="/repo",
    )
    kwargs = dict(
        name="wf", description="d", plan_path="docs/plans/example.md", plan_context=context,
        deliverable_id="dlv-example",
    )
    if shared:
        return emit.compose_script(waves, **kwargs)
    original, original_cap = emit.SharedBlocks, emit._WORKFLOW_SCRIPT_BYTE_CAP
    emit.SharedBlocks = lambda: None  # type: ignore[assignment]
    emit._WORKFLOW_SCRIPT_BYTE_CAP = float("inf")  # type: ignore[assignment]
    try:
        return emit.compose_script(waves, **kwargs)
    finally:
        emit.SharedBlocks, emit._WORKFLOW_SCRIPT_BYTE_CAP = original, original_cap


def test_every_resolved_prompt_is_byte_identical_with_sharing():
    waves = _waves(4)
    assert expand_shared(_compose(waves, shared=True)) == _compose(waves, shared=False)


def test_a_fifty_wave_plan_fits_the_workflow_script_cap():
    waves = _waves(50)
    inline = _compose(waves, shared=False)
    shared = _compose(waves, shared=True)
    assert len(shared.encode("utf-8")) < _WORKFLOW_SCRIPT_CAP
    assert len(shared) < len(inline) / 2
    assert emit._SHARED_VAR + " = [" in shared
