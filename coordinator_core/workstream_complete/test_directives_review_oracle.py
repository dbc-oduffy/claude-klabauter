"""test_directives_review_oracle -- direct unit coverage of the C7
chain-wide oracle arm wired into
`coordinator_core.workstream_complete.directives_review.decide_review_scale`.

Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C7.md

Pins AC11/B4 (plan `docs/plans/2026-08-19-the-baton-carries-its-commits.md`):
the chain-wide arm must NEVER set `partition_mandatory`, even driven to its
ceiling weight -- `ops/ceremony/tail_ops.py` turns `partition_mandatory=True`
plus incomplete review-trail metadata into `failed_critical[]`, the exact
hard stop `state/kill-ledger.md` K-007 removed.
"""

from __future__ import annotations

from coordinator_core.commit_ledger.oracle import OracleFigure, OracleReport
from coordinator_core.workstream_complete.directives_review import (
    _CHAIN_WEIGHT_CEILING,
    decide_review_scale,
)

_NO_REVIEW_KWARGS = dict(
    gross_loc=0,
    code_loc=0,
    commit_count=0,
    surface_count=0,
    executor_dispatched=False,
    shared_schema_touched=False,
    chain_disposition="single-session",
)


def _report(weight: float, resolved: bool = True) -> OracleReport:
    figure = OracleFigure(weight=weight, basis=f"stub basis weight={weight:g}")
    return OracleReport(code_only=figure, with_docs=figure, resolved=resolved)


def test_oracle_report_none_is_byte_identical_to_pre_c7():
    baseline = decide_review_scale(**_NO_REVIEW_KWARGS)
    wired = decide_review_scale(**_NO_REVIEW_KWARGS, oracle_report=None)
    assert baseline == wired
    assert wired.scale == "none"
    assert wired.partition_mandatory is False


def test_arm_raises_no_review_row_to_code_reviewer_at_ceiling():
    decision = decide_review_scale(
        **_NO_REVIEW_KWARGS, oracle_report=_report(_CHAIN_WEIGHT_CEILING)
    )
    assert decision.scale == "code-reviewer"
    assert "chain-wide arm" in decision.reason
    assert decision.partition_mandatory is False
    # Row/partition_mandatory pass through from the core decision untouched.
    assert decision.row == 1


def test_arm_never_sets_partition_mandatory_at_ceiling_weight():
    """AC11/B4: drive the arm to its ceiling weight (and well beyond) and
    assert `partition_mandatory` stays False/unset regardless."""
    for weight in (_CHAIN_WEIGHT_CEILING, _CHAIN_WEIGHT_CEILING * 100, 1e9):
        decision = decide_review_scale(**_NO_REVIEW_KWARGS, oracle_report=_report(weight))
        assert decision.partition_mandatory is False
        assert decision.scale != "partitioned"


def test_arm_never_downgrades_an_unresolved_row4_input():
    kwargs = dict(_NO_REVIEW_KWARGS)
    kwargs["code_loc"] = None
    decision = decide_review_scale(**kwargs, oracle_report=_report(_CHAIN_WEIGHT_CEILING * 10))
    assert decision.scale == "unresolved"
    assert decision.resolved is False
    assert decision.partition_mandatory is False


def test_arm_never_downgrades_or_repartitions_an_already_reviewed_row():
    kwargs = dict(_NO_REVIEW_KWARGS)
    kwargs["executor_dispatched"] = True
    baseline = decide_review_scale(**kwargs)
    decision = decide_review_scale(**kwargs, oracle_report=_report(_CHAIN_WEIGHT_CEILING * 10))
    assert decision == baseline
    assert decision.scale == "code-reviewer"
    assert decision.partition_mandatory is False


def test_arm_does_not_override_row4_partitioned_or_its_mandatory_flag():
    kwargs = dict(_NO_REVIEW_KWARGS)
    # `commit_count` alone no longer trips the brightline off a resolved
    # `code_loc == 0` (2026-08-20, cross-repo/inbox/2026-08-20-example-retrieval-repo-em-
    # review-gate-doc-only-em-discretion.md): the commit arm is a proxy for
    # code risk and a doc-only session has none. This test is about the
    # chain-wide arm never overriding row 4, so it needs a genuine row-4
    # case — code present AND the commit threshold met.
    kwargs["code_loc"] = 120
    kwargs["gross_loc"] = 120
    kwargs["commit_count"] = 5  # trips the row-4 brightline
    baseline = decide_review_scale(**kwargs)
    decision = decide_review_scale(**kwargs, oracle_report=_report(_CHAIN_WEIGHT_CEILING * 10))
    assert decision == baseline
    assert decision.row == 4
    assert decision.scale == "partitioned"
    assert decision.partition_mandatory is True


def test_arm_no_ops_below_ceiling():
    decision = decide_review_scale(
        **_NO_REVIEW_KWARGS, oracle_report=_report(_CHAIN_WEIGHT_CEILING - 0.01)
    )
    assert decision.scale == "none"


def test_arm_no_ops_on_pending_unresolved_oracle_report():
    decision = decide_review_scale(
        **_NO_REVIEW_KWARGS, oracle_report=_report(_CHAIN_WEIGHT_CEILING * 10, resolved=False)
    )
    assert decision.scale == "none"


def test_arm_no_ops_when_oracle_weight_is_none():
    figure = OracleFigure(weight=None, basis="pending")
    report = OracleReport(code_only=figure, with_docs=figure, resolved=False)
    decision = decide_review_scale(**_NO_REVIEW_KWARGS, oracle_report=report)
    assert decision.scale == "none"
