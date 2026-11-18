"""Pins the grind admission arithmetic: run fixed + per batch + per row."""

import pytest

from coordinator_core.grind_triage.contract import default_budget
from coordinator_core.ops.dispatch_emit.grind_admission import (
    ROW_TOTAL_TOKENS,
    TOTAL_PER_OUTPUT_CENTI,
    admit,
    not_admitted_entries,
    receipt_block,
    run_agent_calls,
)

IDS = tuple(f"r{i}" for i in range(61))


def test_charge_covers_the_measured_batched_run() -> None:
    # claude-klabauter 20261114T173532Z: 10 rows, one batch-40 batch, verify-routing batched, 17 calls.
    assert 17 <= run_agent_calls(10, 40) <= 18


def test_larger_batches_admit_more_rows_under_one_cap() -> None:
    small = admit(IDS, max_agent_calls=150, budget_tokens=None, batch_size=4)
    large = admit(IDS, max_agent_calls=150, budget_tokens=None, batch_size=40)
    assert len(large.admitted) > len(small.admitted)


def test_bound_is_the_largest_count_whose_charge_fits() -> None:
    a = admit(IDS, max_agent_calls=32, budget_tokens=None, batch_size=40)
    n = len(a.admitted)
    assert run_agent_calls(n, 40) <= 32 < run_agent_calls(n + 1, 40)


def test_caps_too_small_for_one_row_admit_none() -> None:
    a = admit(IDS, max_agent_calls=run_agent_calls(1, 40) - 1, budget_tokens=None, batch_size=40)
    assert a.admitted == () and a.not_admitted == IDS and a.arithmetic["bound_by_calls"] == 0


def test_both_none_admits_all() -> None:
    a = admit(IDS, max_agent_calls=None, budget_tokens=None)
    assert a.admitted == IDS and a.not_admitted == ()


def test_token_bound_and_min_of_bounds() -> None:
    budget = 7 * ROW_TOTAL_TOKENS + 1
    a = admit(IDS, max_agent_calls=None, budget_tokens=budget)
    assert len(a.admitted) == 7 == a.arithmetic["bound_by_tokens"]
    assert len(admit(IDS, max_agent_calls=500, budget_tokens=budget, batch_size=40).admitted) == 7


def test_million_token_budget_admits_several_rows_not_one() -> None:
    # example-market-data-repo 20261118T182350Z: one row cost 107,967 post-a722ba; 1M fit about 9, was admitting 1.
    a = admit(IDS[:10], max_agent_calls=None, budget_tokens=1_000_000)
    assert a.arithmetic["bound_by_tokens"] >= 5
    assert len(a.admitted) == a.arithmetic["bound_by_tokens"] < 10


def test_budget_below_measured_cost_admits_no_row() -> None:
    assert admit(IDS[:3], max_agent_calls=None, budget_tokens=107_967 - 1).admitted == ()


def test_row_charge_covers_the_measured_post_a722ba_row() -> None:
    # example-market-data-repo 20261118T182350Z: 107,967 total over 2 agent calls, one row.
    assert ROW_TOTAL_TOKENS >= 107_967
    assert ROW_TOTAL_TOKENS == -(-107_967 * 3 // 2)
    # DoE 20261116: 783,824 total / 44,000 output over 3 rows fixes the in-run total-per-output ratio.
    assert TOTAL_PER_OUTPUT_CENTI * 44000 >= 783824 * 100


def test_arithmetic_names_the_total_unit() -> None:
    a = admit(IDS, max_agent_calls=None, budget_tokens=None)
    assert a.arithmetic["row_total_tokens"] == ROW_TOTAL_TOKENS and "row_output_tokens" not in a.arithmetic


def test_none_budget_substitutes_row_scaled_default() -> None:
    a = admit(IDS, max_agent_calls=None, budget_tokens=None)
    assert a.arithmetic["budget_source"] == "default"
    assert a.arithmetic["budget_tokens"] == default_budget(len(IDS))
    assert a.arithmetic["bound_by_tokens"] >= len(IDS) and a.admitted == IDS


def test_explicit_budget_overrides_default_and_is_recorded() -> None:
    a = admit(IDS, max_agent_calls=None, budget_tokens=7 * ROW_TOTAL_TOKENS + 1)
    assert a.arithmetic["budget_source"] == "explicit" and a.arithmetic["budget_tokens"] == 7 * ROW_TOTAL_TOKENS + 1
    assert len(a.admitted) == 7


def test_default_budget_is_not_named_as_the_cut_cap() -> None:
    a = admit(IDS, max_agent_calls=32, budget_tokens=None, batch_size=40)
    assert a.not_admitted
    reason = not_admitted_entries(a)[0]["reason"]
    assert "max_agent_calls=32" in reason and "budget_tokens" not in reason
    b = admit(IDS, max_agent_calls=None, budget_tokens=1_000_000)
    assert "budget_tokens=1000000" in not_admitted_entries(b)[0]["reason"]


def test_order_preserved_no_loss() -> None:
    a = admit(IDS, max_agent_calls=40, budget_tokens=None, batch_size=40)
    assert a.admitted + a.not_admitted == IDS


@pytest.mark.parametrize("kw", [{"max_agent_calls": 0, "budget_tokens": None},
                                {"max_agent_calls": None, "budget_tokens": -1},
                                {"max_agent_calls": 40, "budget_tokens": None, "batch_size": 0}])
def test_non_positive_refused(kw) -> None:
    with pytest.raises(ValueError):
        admit(IDS, **kw)


def test_receipt_block_names_batch_shape() -> None:
    a = admit(IDS, max_agent_calls=40, budget_tokens=None, batch_size=40)
    b = receipt_block(a)
    assert b["count"] == len(IDS) - len(a.admitted) and b["row_ids"] == list(a.not_admitted)
    assert b["arithmetic"]["batch_size"] == 40 and b["arithmetic"]["admitted_count"] == len(a.admitted)
