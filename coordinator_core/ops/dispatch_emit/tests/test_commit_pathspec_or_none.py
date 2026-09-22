"""
coordinator_core.ops.dispatch_emit.tests.test_commit_pathspec_or_none

Purpose: `pathspec.commit_pathspec_or_none` is the name DoE's
`emit-dispatch-workflow.py` probes with `hasattr` to decide whether the engine
emits a solitary `writes: []` wave itself. While no engine defined it the probe
read False everywhere, the caller's drop-filter ran instead, and a plan whose
verification row is named in another row's `depends_on` could not be emitted at
all (claude-klabauter#35). The name is therefore a cross-repo contract, not an
internal helper.

Scope: the primitive's own refusal shapes are pinned next door in
`test_pathspec.py`. What lives here is what that file cannot see -- the wiring
through `emit.compose_script`, which is where #35 actually bit and where a
defined-but-unwired primitive still reproduces the bug.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/dispatch_emit/tests/test_commit_pathspec_or_none.py -q
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.pathspec import (
    commit_pathspec,
    commit_pathspec_or_none,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _row(row_id: str, writes, depends_on=()) -> WaveRow:
    return WaveRow(
        id=row_id,
        title=f"row {row_id}",
        surface="dispatch_emit",
        writes=writes,
        reads=[],
        depends_on=list(depends_on),
    )


def test_a_mixed_wave_keeps_its_commit_phase():
    """One contributing row is enough: the wave still commits, and commits
    exactly what `commit_pathspec` would have returned unaided."""
    wave = [_row("C1", []), _row("C2", ["a/b.py"])]

    assert commit_pathspec_or_none(wave) == commit_pathspec(wave)


def test_a_depended_on_verification_row_emits_instead_of_refusing_the_plan():
    """claude-klabauter#35's reported shape, end to end.

    This is the assertion that fails against an engine which defines
    `commit_pathspec_or_none` but leaves `compose_script` routing through a
    caller-side pre-check instead -- the primitive passes its own unit tests
    there while the plan stays unemittable.
    """
    waves = [[_row("C2", [])], [_row("C3", ["a/b.py"], depends_on=["C2"])]]

    script = compose_script(waves, name="wf", description="verification row gates C3")

    assert "C2" in script and "C3" in script
    assert "commit phase omitted" in script
