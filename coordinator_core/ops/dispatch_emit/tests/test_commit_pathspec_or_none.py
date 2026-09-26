
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
    wave = [_row("C1", []), _row("C2", ["a/b.py"])]

    assert commit_pathspec_or_none(wave) == commit_pathspec(wave)


def test_a_depended_on_verification_row_emits_instead_of_refusing_the_plan():
    waves = [[_row("C2", [])], [_row("C3", ["a/b.py"], depends_on=["C2"])]]

    script = compose_script(waves, name="wf", description="verification row gates C3")

    assert "C2" in script and "C3" in script
    assert "commit phase omitted" in script
