"""The scope floor raises novel or coupled scope to L but never a mechanically-uniform one."""

import pytest

from coordinator_core import sizing_assemble as sa

_M = {"tshirt": "M"}
_REASON = "same capture re-run over every page"


def test_uniform_large_scope_stays_below_l():
    decision = sa.route(
        estimate=_M,
        scope_files=11676,
        scope_lines=50000,
        mechanically_uniform=True,
        mechanically_uniform_reason=_REASON,
    )
    assert decision["resolved_estimate"]["tshirt"] == "M"
    assert decision["route"] != "spec-dispatch"
    assert "scope floor" not in (decision["tie_break"] or "")


@pytest.mark.parametrize("kwargs", [{"scope_files": 12}, {"scope_lines": 2000}])
def test_non_uniform_large_scope_still_floors_to_l(kwargs):
    decision = sa.route(estimate=_M, **kwargs)
    assert decision["resolved_estimate"]["tshirt"] == "L"
    assert "scope floor" in decision["tie_break"]


def test_uniform_flag_without_reason_still_refused():
    with pytest.raises(sa.SizingAssembleError):
        sa.route(estimate=_M, scope_files=40, mechanically_uniform=True)
