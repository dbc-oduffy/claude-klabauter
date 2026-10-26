"""Pins the run-truth contract names DoE adopts, and mandatory_stages' fail-safe."""

import dataclasses

import pytest

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import emit, run_truth_contract as c


def _fields(cls):
    return tuple(f.name for f in dataclasses.fields(cls))


def test_names_are_pinned():
    assert c.PART_STATUSES == ("open", "partial", "met", "deferred", "waived", "unwired")
    assert c.PART_STATUSES[:5] == tuple(rr.STATUSES)
    assert c.CLOSE_OUTCOMES == ("done", "partial", "refused")
    assert c.MANDATORY_STAGES == ("sizing", "plan_review", "execute", "review", "kira", "terminal_judge", "reacher")
    assert c.NOT_APPLICABLE == "not_applicable"
    assert c.RECEIPT_PATH_TEMPLATE == "state/warp/{run_id}/receipts/{stage}.json"
    assert _fields(c.Receipt) == c.RECEIPT_FIELDS == ("stage", "run_id", "verdict", "artifact", "at")
    assert _fields(c.Part) == ("id", "source_text", "anchor", "status", "evidence")
    assert _fields(c.Evidence) == c.PART_EVIDENCE_KEYS
    assert _fields(c.Close) == ("outcome", "open_parts", "missing_receipts")
    assert set(_fields(c.MutationReply)) | {"ok"} == set(c.MUTATION_REPLY_FIELDS)
    assert c.SUITE_VERDICT_PATH_TEMPLATE == "state/warp/suite-verdicts/{sha}.json"
    assert set(_fields(c.SuiteVerdict)) == set(c.SUITE_VERDICT_FIELDS) - {"reds"} | {"reds"}
    assert c.ANCHOR_OUTCOMES == ("held", "broke", "unevaluable")
    assert _fields(c.AnchorVerdict) == ("id", "outcome", "observed", "expect")
    assert _fields(c.ReviewVerdict) == ("verdict", "fixes_applied", "reverified")
    assert _fields(c.SizingFingerprint) == ("parts_count", "tshirt_floor", "rows")


def test_part_replays_cover_every_part():
    assert set(c.PART_REPLAYS) == {
        "P-A", "P-B", "P-C", "P-D", "P-F1", "P-F2", "P-F3", "P-F17", "P-G6", "P-H9", "P-OWED", "P-REG4",
    }
    assert all(paths for paths in c.PART_REPLAYS.values())


def test_part_closes_only_with_full_evidence():
    ev = c.Evidence("abc", "t.py::t", "pass")
    assert c.Part("R2", "x", status="met", evidence=ev).closed()
    assert not c.Part("R2", "x", status="partial", evidence=ev).closed()
    assert not c.Part("R2", "x", status="met", evidence=c.Evidence("abc", "t", "fail")).closed()
    assert not c.Part("R2", "x", status="met").closed()


def test_close_verdict_partial_names_open_rows_and_missing_receipts():
    done = c.Part("R2", "x", status="met", evidence=c.Evidence("a", "t", "pass"))
    open_ = c.Part("R3", "y")
    got = c.close_verdict([done, open_], ["sizing"], ["sizing", "kira"])
    assert got == c.Close("partial", ("R3",), ("kira",))
    assert c.close_verdict([done], ["sizing"], ["sizing"]).outcome == "done"


def test_mutation_reply_ok_requires_applied():
    assert not c.MutationReply(refused=({"input": "R9", "reason": "arm cannot consume"},)).ok
    assert c.MutationReply(applied=("R9",)).ok


def test_suite_verdict_green_needs_grant_and_no_reds():
    assert c.SuiteVerdict("s", 0, 10, 1, grant_ref="g").green()
    assert not c.SuiteVerdict("s", 0, 10, 1).green()
    assert not c.SuiteVerdict("s", 1, 10, 1, grant_ref="g", reds=({"nodeid": "n", "first_red_commit": "c"},)).green()


@pytest.mark.parametrize("arm", [None, "bogus", *c.ARMS])
def test_unknown_size_makes_every_stage_mandatory(arm):
    assert c.mandatory_stages(arm, None) == c.MANDATORY_STAGES
    assert c.mandatory_stages(arm, "standard") == c.MANDATORY_STAGES


def test_unknown_arm_with_known_size_is_fail_safe():
    assert c.mandatory_stages(None, "XS") == c.MANDATORY_STAGES


def test_kira_follows_tshirt_not_review_tier_label():
    # register #20: the review-tier label calls L "standard"; Kira is still mandatory on L.
    assert emit._TSHIRT_TO_REVIEW_TIER["L"] == "standard"
    assert c.STAGE_KIRA in c.mandatory_stages(c.ARM_M_PLUS, "L")
    assert c.STAGE_KIRA not in c.mandatory_stages(c.ARM_M_PLUS, "M")
    for t in ("XL", "XXL"):
        assert c.STAGE_KIRA in c.mandatory_stages(c.ARM_M_PLUS, t)


def test_known_cells_never_drop_judge_and_are_ordered_subsets():
    for arm, tshirt in [("xs", "XS"), ("s", "S"), ("m_plus", "M"), ("m_plus", "L"), ("roadmap", "XL")]:
        got = c.mandatory_stages(arm, tshirt)
        assert c.STAGE_TERMINAL_JUDGE in got and c.STAGE_EXECUTE in got
        assert list(got) == [s for s in c.MANDATORY_STAGES if s in got]
