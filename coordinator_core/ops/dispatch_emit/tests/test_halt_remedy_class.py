"""Each UE-retrieval halt kind classifies in halt_remedy_class and validates in the vendored wake-digest schema."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc
from coordinator_core.ops.dispatch_emit import wake_digest as wd

UE_KINDS = (
    "ue_retrieval_missing",
    "ue_retrieval_check_failed",
    "example_retrieval_repo_outage_reported",
    "ue_phase_retrieval_missing",
)


def _digest(**extra) -> dict:
    d = {
        "schema": "doe-wake-digest", "version": 1, "kind": "close",
        "plan": {"path": "docs/plans/x.md", "deliverable_id": None},
        "outcome": "complete",
        "criterion": {"status": "met", "observation": None, "sidecar": None},
        "decision_required": None, "em_may_think_differently": [], "overflow": 0,
        "deviations": [], "assembled_by": "code",
        "counts": {"reviewers": 0, "findings_applied": 0, "escalations": 0, "preflight_findings": 0},
        "next_action": {"kind": "none", "op": None, "params": None},
    }
    d.update(extra)
    return d


def _halted_by(kind: str, **overrides) -> dict:
    h = {
        "halted": kind, "cause": kind, "friction_count": 1, "reports": ["example-retrieval-repo is down"],
        "agents": [{
            "agent_id": "a1", "agent_type": "coordinator:sizing-scout", "label": "sizing",
            "rag_calls": 0, "ue_calls": 0, "denies": 1, "metrics_rows": 1,
        }],
    }
    h.update(overrides)
    return h


def test_constants_carry_the_kind_strings() -> None:
    assert (
        hrc.HALT_UE_RETRIEVAL_MISSING,
        hrc.HALT_UE_RETRIEVAL_CHECK_FAILED,
        hrc.HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED,
        hrc.HALT_UE_PHASE_RETRIEVAL_MISSING,
    ) == UE_KINDS


@pytest.mark.parametrize("kind", UE_KINDS)
def test_kind_classifies_as_decision_with_an_irreversible_reason(kind: str) -> None:
    assert hrc.HALT_REMEDY[kind] == (hrc.DECISION, None)
    assert not hrc.is_mechanical(kind)
    assert hrc.is_irreversible(kind)
    with pytest.raises(ValueError):
        hrc.remedy_or_halt(kind, lambda n: n, lambda f: f)


@pytest.mark.parametrize("kind", UE_KINDS)
def test_kind_validates_against_the_wake_digest_schema(kind: str) -> None:
    assert wd.validate_doe_digest(_digest(halted_by=_halted_by(kind))) == []


@pytest.mark.parametrize("kind", UE_KINDS)
def test_a_null_agent_type_validates(kind: str) -> None:
    h = _halted_by(kind)
    h["agents"][0]["agent_type"] = None
    assert wd.validate_doe_digest(_digest(halted_by=h)) == []


def test_an_unregistered_kind_is_still_refused_by_the_schema() -> None:
    assert wd.validate_doe_digest(_digest(halted_by=_halted_by("ue_invented_kind"))) != []
    assert wd.validate_doe_digest(_digest(halted_by=_halted_by(UE_KINDS[0], invented=1))) != []
