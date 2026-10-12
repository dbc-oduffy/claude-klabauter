"""Contract invariants: unique tuples, non-empty reasons, RuleResult refuses unlisted reasons."""

import pytest

from coordinator_core.workweek_complete.release.contract import (
    FORBIDDEN_ARGV_TOKENS,
    GATE_IDS,
    MISS_REASONS,
    GateRecord,
    StampResult,
    RELEASE_MODES,
    RELEASE_STATES,
    RULE_IDS,
    ReleaseState,
    RuleResult,
)


@pytest.mark.parametrize("seq", [RULE_IDS, RELEASE_STATES, RELEASE_MODES, FORBIDDEN_ARGV_TOKENS])
def test_tuples_are_unique(seq):
    assert isinstance(seq, tuple) and len(set(seq)) == len(seq)


def test_every_rule_has_nonempty_unique_reasons():
    assert set(MISS_REASONS) == set(RULE_IDS)
    for rule, reasons in MISS_REASONS.items():
        assert reasons and len(set(reasons)) == len(reasons), rule


def test_pass_has_empty_reason():
    assert RuleResult("version-bump", True).reason == ""
    with pytest.raises(ValueError):
        RuleResult("version-bump", True, "tag-exists")


def test_miss_accepts_listed_reason_only():
    assert not RuleResult("version-bump", False, "tag-exists", {"tag": "v1"}).passed
    with pytest.raises(ValueError):
        RuleResult("version-bump", False, "uncovered-commits")
    with pytest.raises(ValueError):
        RuleResult("version-bump", False, "")


def test_gate_ids_unique_tuple():
    assert isinstance(GATE_IDS, tuple) and len(set(GATE_IDS)) == len(GATE_IDS)
    assert GATE_IDS == ("d6", "d0", "validate-fast-and-packageability")


@pytest.mark.parametrize("reason", ["head-moved", "tree-modified"])
def test_merge_evidence_refuses_retired_reasons(reason):
    with pytest.raises(ValueError):
        RuleResult("merge-evidence", False, reason)


def test_merge_evidence_accepts_candidate_not_on_branch():
    assert not RuleResult("merge-evidence", False, "candidate-not-on-branch").passed


def test_gate_record_accepts_valid_and_refuses_bad_fields():
    rec = GateRecord("d6", "ran", 0, "abc123")
    assert rec.tested_sha == "abc123"
    with pytest.raises(ValueError):
        GateRecord("nope", "ran", 0, "abc")
    with pytest.raises(ValueError):
        GateRecord("d6", "nope", 0, "abc")
    with pytest.raises(ValueError):
        GateRecord("d6", "ran", 0, "")


def test_stamp_result_changed_is_tuple_default_empty():
    assert StampResult().changed == ()


def test_unknown_rule_and_state_refused():
    with pytest.raises(ValueError):
        RuleResult("nope", True)
    with pytest.raises(ValueError):
        ReleaseState("nope")
