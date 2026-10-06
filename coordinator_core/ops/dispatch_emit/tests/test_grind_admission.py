"""Pins the grind admission arithmetic."""

import pytest

from coordinator_core.ops.dispatch_emit.grind_admission import admit, receipt_block

IDS = tuple(f"r{i}" for i in range(21))


def test_calls_bound_admits_first_12_of_21():
    a = admit(IDS, max_agent_calls=40, budget_tokens=None)
    assert a.admitted == IDS[:12]
    assert a.not_admitted == IDS[12:]
    assert len(a.not_admitted) == 9


def test_calls_boundary_67_all_66_twenty():
    a = admit(IDS, max_agent_calls=67, budget_tokens=None)
    assert a.admitted == IDS and a.not_admitted == ()
    assert len(admit(IDS, max_agent_calls=66, budget_tokens=None).admitted) == 20


def test_both_none_admits_all():
    a = admit(IDS, max_agent_calls=None, budget_tokens=None)
    assert a.admitted == IDS and a.not_admitted == ()


def test_token_bound():
    a = admit(IDS, max_agent_calls=200, budget_tokens=1_000_000)
    assert len(a.admitted) == 7 == 1_000_000 // 140076
    assert a.arithmetic["bound_by_tokens"] == 7


def test_min_of_bounds():
    assert len(admit(IDS, max_agent_calls=40, budget_tokens=1_000_000).admitted) == 7
    assert len(admit(IDS, max_agent_calls=10, budget_tokens=10_000_000).admitted) == 3


def test_order_preserved_no_loss():
    a = admit(IDS, max_agent_calls=40, budget_tokens=None)
    assert a.admitted + a.not_admitted == IDS


@pytest.mark.parametrize("kw", [{"max_agent_calls": 0, "budget_tokens": None},
                                {"max_agent_calls": None, "budget_tokens": -1}])
def test_non_positive_refused(kw):
    with pytest.raises(ValueError):
        admit(IDS, **kw)


def test_receipt_block():
    b = receipt_block(admit(IDS, max_agent_calls=40, budget_tokens=None))
    assert b["count"] == 9 and b["row_ids"] == list(IDS[12:])
    assert b["arithmetic"]["admitted_count"] == 12
