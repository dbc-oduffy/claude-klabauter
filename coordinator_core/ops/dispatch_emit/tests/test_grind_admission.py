"""Pins the grind admission arithmetic: run fixed + per batch + per row."""

import pytest

from coordinator_core.ops.dispatch_emit.grind_admission import admit, receipt_block, run_agent_calls

IDS = tuple(f"r{i}" for i in range(61))


def test_charge_covers_the_measured_batched_run():
    # claude-klabauter 20261114T173532Z: 10 rows, one batch-40 batch, verify-routing batched, 17 calls.
    assert 17 <= run_agent_calls(10, 40) <= 18


def test_larger_batches_admit_more_rows_under_one_cap():
    small = admit(IDS, max_agent_calls=150, budget_tokens=None, batch_size=4)
    large = admit(IDS, max_agent_calls=150, budget_tokens=None, batch_size=40)
    assert len(large.admitted) > len(small.admitted)


def test_bound_is_the_largest_count_whose_charge_fits():
    a = admit(IDS, max_agent_calls=32, budget_tokens=None, batch_size=40)
    n = len(a.admitted)
    assert run_agent_calls(n, 40) <= 32 < run_agent_calls(n + 1, 40)


def test_caps_too_small_for_one_row_admit_none():
    a = admit(IDS, max_agent_calls=run_agent_calls(1, 40) - 1, budget_tokens=None, batch_size=40)
    assert a.admitted == () and a.not_admitted == IDS and a.arithmetic["bound_by_calls"] == 0


def test_both_none_admits_all():
    a = admit(IDS, max_agent_calls=None, budget_tokens=None)
    assert a.admitted == IDS and a.not_admitted == ()


def test_token_bound_and_min_of_bounds():
    a = admit(IDS, max_agent_calls=None, budget_tokens=1_000_000)
    assert len(a.admitted) == 7 == a.arithmetic["bound_by_tokens"]
    assert len(admit(IDS, max_agent_calls=500, budget_tokens=1_000_000, batch_size=40).admitted) == 7


def test_order_preserved_no_loss():
    a = admit(IDS, max_agent_calls=40, budget_tokens=None, batch_size=40)
    assert a.admitted + a.not_admitted == IDS


@pytest.mark.parametrize("kw", [{"max_agent_calls": 0, "budget_tokens": None},
                                {"max_agent_calls": None, "budget_tokens": -1},
                                {"max_agent_calls": 40, "budget_tokens": None, "batch_size": 0}])
def test_non_positive_refused(kw):
    with pytest.raises(ValueError):
        admit(IDS, **kw)


def test_receipt_block_names_batch_shape():
    a = admit(IDS, max_agent_calls=40, budget_tokens=None, batch_size=40)
    b = receipt_block(a)
    assert b["count"] == len(IDS) - len(a.admitted) and b["row_ids"] == list(a.not_admitted)
    assert b["arithmetic"]["batch_size"] == 40 and b["arithmetic"]["admitted_count"] == len(a.admitted)
