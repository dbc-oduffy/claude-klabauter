"""Contract invariants: unique tuples, non-empty reasons, RuleResult refuses unlisted reasons."""

import pytest

from coordinator_core.workweek_complete.release.contract import (
    FORBIDDEN_ARGV_TOKENS,
    MISS_REASONS,
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


def test_unknown_rule_and_state_refused():
    with pytest.raises(ValueError):
        RuleResult("nope", True)
    with pytest.raises(ValueError):
        ReleaseState("nope")
