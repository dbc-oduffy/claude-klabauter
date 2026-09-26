
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.emit import (
    MixedAgentTypeRowError,
    UnroutableWorkKindRowError,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_EVIDENCE = "docs/plans/evidence/2026-09-11-opus-model-tier-arm-results.md"
_BODY = "Spec: measure the arms\nComplexity: M\n"


def _row(writes, *, change_kind=None, agent_type=None, body=_BODY):
    return WaveRow(
        id="C4",
        title="measure",
        surface="personas",
        writes=writes,
        reads=[],
        depends_on=[],
        agent_type=agent_type,
        change_kind=change_kind,
        body=body,
    )


def test_an_override_no_longer_silences_the_unroutable_diagnostic():
    with pytest.raises(UnroutableWorkKindRowError) as excinfo:
        compose_script(
            [[_row([_EVIDENCE], change_kind="verification",
                   agent_type="coordinator:enricher")]],
            name="wf",
            description="c4",
        )
    assert "Split the row" in str(excinfo.value)


def test_the_same_row_without_an_override_still_raises():
    with pytest.raises(UnroutableWorkKindRowError):
        compose_script(
            [[_row([_EVIDENCE], change_kind="verification")]],
            name="wf",
            description="c4-bare",
        )


def test_an_override_on_a_routable_body_row_is_still_honoured():
    script = compose_script(
        [[_row([_EVIDENCE], agent_type="coordinator:enricher")]],
        name="wf",
        description="routable",
    )
    assert "agentType: 'coordinator:enricher'" in script


def test_mixed_writes_still_outrank_an_override():
    with pytest.raises(MixedAgentTypeRowError):
        compose_script(
            [[_row([_EVIDENCE, "coordinator_core/ops/dispatch_emit/emit.py"],
                   agent_type="coordinator:enricher")]],
            name="wf",
            description="mixed",
        )
