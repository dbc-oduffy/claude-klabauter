"""A test leg that returned no review never marks an em-discharged met requirement unmet."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import land_run

LEG_DOWN = {"wave_not_run": [{"slot": -1, "agent_type": "dispatch.test_leg", "reason": "returned no review"}]}
ITEMS = [
    {"id": "R1", "status": "met", "met_by": ["C4"]},
    {"id": "R2", "status": "met", "met_by": ["C9"]},
    {"id": "R3", "status": "not_met"},
]
RESUME = {"em_discharged": {"C4": "EM ran the tests: 210 green at the land"}}
REASON = "test-leg-error: dispatch.test_leg returned no review; the measurement is stale, not the requirement"


def test_em_discharged_met_item_is_not_staled_by_the_leg_fault() -> None:
    out = land_run.leg_stale_met_failures(ITEMS, REASON, LEG_DOWN, RESUME)
    assert [f["id"] for f in out] == ["R2"]
    assert out[0]["cause"] == land_run.TEST_LEG_INFRA_CAUSE


def test_every_met_item_is_credited_leaves_no_failure() -> None:
    resume = {"em_discharged": {"C4": "done", "C9": "done"}}
    assert land_run.leg_stale_met_failures(ITEMS, REASON, LEG_DOWN, resume) == []


def test_no_leg_fault_keeps_the_drift_failure_for_every_met_item() -> None:
    out = land_run.leg_stale_met_failures(ITEMS, "measured-rev-drift: a.py", {}, RESUME)
    assert [f["id"] for f in out] == ["R1", "R2"]
    assert all("cause" not in f for f in out)


def test_unevidenced_item_carries_the_typed_test_leg_infra_cause() -> None:
    out = land_run.leg_stale_met_failures(ITEMS, REASON, LEG_DOWN, None)
    assert {f["cause"] for f in out} == {"test_leg_infra"}
    assert "Re-run the review stage's test leg" in land_run.TEST_LEG_INFRA_RETRY
