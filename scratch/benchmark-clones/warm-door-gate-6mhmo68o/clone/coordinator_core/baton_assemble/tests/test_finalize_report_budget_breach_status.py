"""Regression coverage for the C10 downstream defect: `_finalize_report`
stamping `status: "claim_denied"` on a composition-budget pre-mutation
breach, which reuses `APPLY_EXIT_CLAIM_DENIED`'s numeric position
(`coordinator_core.contract.apply_base.execute_directives`, chunk C10,
docs/plans/2026-08-15-composition-invocation-budgets.md) but is not
actually a claim denial -- no claim was evaluated.

Spec backlink: docs/plans/2026-08-15-composition-invocation-budgets.md, chunk C10
"""

from __future__ import annotations

from coordinator_core.baton_assemble import apply as ba_apply


def test_a_budget_breach_report_is_not_mislabelled_claim_denied():
    exit_code = ba_apply.apply_base.APPLY_EXIT_CLAIM_DENIED
    report = {"landed": [], "budget_breach": "composition budget breach: ..."}

    _, finalized = ba_apply._finalize_report(exit_code, report)

    assert finalized["status"] != "claim_denied"
    assert finalized["status"] == ba_apply._BUDGET_BREACH_STATUS


def test_a_genuine_claim_denial_is_still_labelled_claim_denied():
    exit_code = ba_apply.apply_base.APPLY_EXIT_CLAIM_DENIED
    report = {"landed": [], "claim_grant": {"verdict": "denied"}}

    _, finalized = ba_apply._finalize_report(exit_code, report)

    assert finalized["status"] == "claim_denied"
