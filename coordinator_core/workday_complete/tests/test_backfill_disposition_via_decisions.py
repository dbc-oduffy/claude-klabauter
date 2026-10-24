"""A backfill-cap disposition in apply's decisions opens phase B; no decision keeps it blocked."""

from __future__ import annotations

import pytest

from coordinator_core.ops.workday_complete_backfill_scan import GapScan
from coordinator_core.workday_complete import apply as wc_apply
from coordinator_core.workday_complete import brief as wc_brief
from coordinator_core.workday_complete import run_spec

_ID = "jp_step3_5_backfill_cap"
_PHASE_B = "d_step3_5_backfill_phase_b"


def _point() -> dict:
    return wc_brief._backfill_cap_judgment_point(GapScan())


def _phase_b() -> dict:
    return {
        "id": _PHASE_B,
        "cli": "workday-complete-close",
        "args": ["backfill-dispatch-rows", "--allow-empty"],
        "depends_on": _ID,
    }


@pytest.fixture
def dispatched(monkeypatch) -> list[str]:
    seen: list[str] = []

    def _recorder(directive, *, stdin_text=None):
        seen.append(directive["id"])
        return {
            "id": directive["id"],
            "cli": directive["cli"],
            "args": list(directive.get("args", [])),
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "exit_class": "returned",
        }

    monkeypatch.setattr(wc_apply, "_dispatch_directive", _recorder)
    return seen


@pytest.mark.parametrize("disposition", ["backfill_all", "bounded_subset"])
def test_a_chosen_disposition_opens_phase_b(dispatched, disposition):
    code, report = wc_apply._execute_directives(
        [_phase_b()], [_point()], {_ID: {"disposition": disposition}}
    )
    assert dispatched == [_PHASE_B]
    assert _PHASE_B not in report["blocked"]
    assert _PHASE_B in report["landed"]
    assert code == int(wc_apply.WorkdayApplyExitCode.SUCCESS)


def test_no_decision_keeps_phase_b_blocked(dispatched):
    code, report = wc_apply._execute_directives([_phase_b()], [_point()], {})
    assert dispatched == []
    assert report["blocked"] == [_PHASE_B]
    assert code == int(wc_apply.WorkdayApplyExitCode.HALTED_AT_JUDGMENT)


def test_driver_rule_decides_backfill_all_for_the_same_point():
    decision = run_spec.backfill_cap(_point(), context={})
    assert decision is not None
    assert decision.decision == "backfill_all"
