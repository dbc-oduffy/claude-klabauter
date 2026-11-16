"""Contract pins: frozen shapes, verdict values, and the row-scaled budget default."""

from __future__ import annotations

import dataclasses

import pytest

from coordinator_core.grind_triage import contract
from coordinator_core.ops.dispatch_emit.grind_admission import ROW_TOTAL_TOKENS, admit


@pytest.mark.parametrize(
    "obj",
    [
        contract.TriageRow(key="k", title="t", is_test=True),
        contract.TriageResult(key="k", verdict=contract.Verdict.SURVIVOR),
        contract.Cluster(signature="s", row_keys=("k",)),
        contract.CheckpointEvent(percent=25, settled=1, total=4),
    ],
)
def test_shapes_are_frozen(obj) -> None:
    field = dataclasses.fields(obj)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, field, "x")


def test_verdict_values_and_checkpoint_percents() -> None:
    assert {v.value for v in contract.Verdict} == {"flake", "not_reproduced", "survivor"}
    assert contract.CHECKPOINT_PERCENTS == (25, 50, 75)


@pytest.mark.parametrize("n", [1, 7, 77])
def test_default_budget_admits_every_row(n) -> None:
    ids = [f"r{i}" for i in range(n)]
    adm = admit(ids, max_agent_calls=None, budget_tokens=contract.default_budget(n))
    assert adm.admitted == tuple(ids)


def test_default_budget_scales_with_rows_and_is_bounded() -> None:
    assert contract.default_budget(10) > contract.default_budget(5) >= 5 * ROW_TOTAL_TOKENS
    assert contract.default_budget(0) > 0
    # Bounded: one fewer row's worth of budget no longer covers all rows.
    short = contract.default_budget(10) - 3 * ROW_TOTAL_TOKENS
    assert admit([str(i) for i in range(10)], max_agent_calls=None, budget_tokens=short).not_admitted


def test_default_budget_is_rows_plus_fixed_reserve_in_total_tokens() -> None:
    from coordinator_core.ops.dispatch_emit.grind_admission import RUN_FIXED_AGENT_CALLS

    assert contract.default_budget(3) == (3 + RUN_FIXED_AGENT_CALLS) * ROW_TOTAL_TOKENS


def test_default_budget_rejects_negative() -> None:
    with pytest.raises(ValueError):
        contract.default_budget(-1)
