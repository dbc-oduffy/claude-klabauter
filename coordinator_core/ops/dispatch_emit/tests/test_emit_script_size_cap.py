"""
Tests for the Workflow-runner script-size refusal (compose_script /
`_WORKFLOW_SCRIPT_BYTE_CAP`).

Spec backlink: state/bug-backlog/2026-09-23-dispatch-emit-writes-a-workflow-
script-t-10ad124c7958.yaml.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.emit import (
    NoWavesError,
    _WORKFLOW_SCRIPT_BYTE_CAP,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _wave_row(id_, writes):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface="dispatch_emit",
        writes=writes,
        reads=[],
        depends_on=[],
        agent_type=None,
        agent_model=None,
    )


def _n_row_waves(n: int) -> list:
    """`n` single-row waves, each writing its own distinct path -- the
    smallest fixture shape existing dispatch_emit tests build a spine from
    (`test_emit.py::_two_wave_fixture`), repeated to grow the composed
    script past the cap.
    """
    return [
        [_wave_row(f"C{i}", [f"coordinator_core/ops/dispatch_emit/fixture_{i}.py"])]
        for i in range(n)
    ]


def test_compose_script_refuses_over_the_runner_byte_cap():
    waves = _n_row_waves(150)

    with pytest.raises(NoWavesError) as excinfo:
        compose_script(waves, name="big", description="oversized inventory")

    message = str(excinfo.value)
    assert str(_WORKFLOW_SCRIPT_BYTE_CAP) in message
    assert "150" in message
    assert "split the inventory into parts of fewer rows" in message


def test_compose_script_emits_fine_under_the_runner_byte_cap():
    waves = _n_row_waves(2)

    script = compose_script(waves, name="small", description="small inventory")

    assert len(script.encode("utf-8")) <= _WORKFLOW_SCRIPT_BYTE_CAP
