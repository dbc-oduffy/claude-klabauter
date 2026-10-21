"""handoff_state_refusal: the one deployment_state predicate (leg (c)) for handoffs."""

import pytest

from coordinator_core.ops.deliverable_cascade import handoff_state_refusal


@pytest.mark.parametrize("state", ["ready_to_fire", "in_flight"])
def test_live_states_clear(state):
    assert handoff_state_refusal({"deployment_state": state}) is None


@pytest.mark.parametrize(
    "gate",
    [
        {"gate_dependency": "x"},
        {"blocked_by": ["other"]},
        {"blocking_notes": "waiting on a real thing"},
    ],
)
def test_gated_awaiting_gate_refuses(gate):
    msg = handoff_state_refusal({"deployment_state": "awaiting_gate", **gate})
    assert msg is not None and "not consistent with terminal" in msg
    assert "'awaiting_gate'" in msg


@pytest.mark.parametrize(
    "extra", [{}, {"blocking_notes": ""}, {"blocking_notes": "PLACEHOLDER: fill in"}]
)
def test_ungated_awaiting_gate_clears(extra):
    assert handoff_state_refusal({"deployment_state": "awaiting_gate", **extra}) is None


def test_missing_deployment_state_refuses():
    msg = handoff_state_refusal({})
    assert msg is not None and "None" in msg


def test_shipped_refuses():
    assert "not consistent with terminal" in handoff_state_refusal(
        {"deployment_state": "shipped"}
    )
