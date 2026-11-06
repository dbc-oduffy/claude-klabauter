"""clean_clone_proof_contract: constants, verdict JSON shape, criterion only on failure."""

from __future__ import annotations

import dataclasses

import pytest

from coordinator_core.bug_blitz import contract as bb
from coordinator_core.ops import clean_clone_proof_contract as cc
from coordinator_core.ops.dispatch_emit import run_truth_contract as rt


def _verdict(**over):
    base = dict(
        sha="abc123",
        run_id="r1",
        run_base_sha="base000",
        selected=("t/test_a.py",),
        outcome=bb.OUTCOME_FAILED,
        failing_ids=("t/test_a.py::test_x",),
        masked_paths=("pkg/mod.py",),
    )
    base.update(over)
    return cc.CleanCloneVerdict(**base)


def test_constants():
    assert cc.REASON_RED_AT_SHA == "red-at-sha"
    assert cc.RUNNER_MODULE == "coordinator_core.ops.clean_clone_proof"
    assert cc.VERDICT_PATH_TEMPLATE == "state/warp/clean-clone-proofs/{sha}.json"
    assert cc.verdict_path("abc") == "state/warp/clean-clone-proofs/abc.json"
    sibling = rt.SUITE_VERDICT_PATH_TEMPLATE.replace("suite-verdicts", "clean-clone-proofs")
    assert cc.VERDICT_PATH_TEMPLATE == sibling


def test_outcomes_reuse_bug_blitz_vocabulary():
    assert set(cc.VERDICT_OUTCOMES) == {
        bb.OUTCOME_PASSED,
        bb.OUTCOME_FAILED,
        bb.OUTCOME_NOTHING_TO_RUN,
        bb.OUTCOME_NO_RUNNER,
    }


def test_verdict_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _verdict().outcome = bb.OUTCOME_PASSED


def test_to_json_shape():
    assert _verdict().to_json() == {
        "sha": "abc123",
        "run_id": "r1",
        "run_base_sha": "base000",
        "selected": ["t/test_a.py"],
        "outcome": "failed",
        "failing_ids": ["t/test_a.py::test_x"],
        "masked_paths": ["pkg/mod.py"],
    }


def test_criterion_for_failed_names_failures_and_masked_paths():
    assert cc.criterion_for(_verdict()) == {
        "status": "not_met",
        "reason": "red-at-sha",
        "sha": "abc123",
        "failing": ["t/test_a.py::test_x"],
        "masked_paths": ["pkg/mod.py"],
    }


@pytest.mark.parametrize(
    "outcome", [bb.OUTCOME_PASSED, bb.OUTCOME_NOTHING_TO_RUN, bb.OUTCOME_NO_RUNNER]
)
def test_criterion_is_none_unless_failed(outcome):
    assert cc.criterion_for(_verdict(outcome=outcome, failing_ids=())) is None
