"""mint_refusal mirrors review_stamp.mint's ladder; judge_verdict arms."""

from __future__ import annotations

import copy

from coordinator_core.completion_receipts.verdict import judge_verdict, mint_refusal

NOW = "2026-10-01T12:00:00Z"


def _record() -> dict:
    return {
        "delivery": {"verdict": "PASS"},
        "unresolved": [],
        "confinement_violations": 0,
        "criterion": {"status": "met", "observation": "ran and held"},
        "tests": {"status": "pass"},
        "prep": {"slice_files": ["a.py"], "product_files": ["a.py"], "foreign_claims": []},
    }


def _refusal(rec: dict) -> str | None:
    return mint_refusal(rec, rec["prep"], rec["tests"])


def test_clean_record_is_not_refused():
    assert _refusal(_record()) is None


def test_delivery_not_pass():
    rec = _record()
    rec["delivery"]["verdict"] = "FAIL"
    assert _refusal(rec) == "review-stamp: refusing to mint: delivery verdict is 'FAIL', not PASS"


def test_criterion_not_met_and_indeterminate():
    for status in ("not_met", "indeterminate"):
        rec = _record()
        rec["criterion"]["status"] = status
        assert _refusal(rec) == f"review-stamp: refusing to mint: exit criterion is {status}"


def test_tests_not_pass():
    rec = _record()
    rec["tests"]["status"] = "fail"
    assert _refusal(rec) == "review-stamp: refusing to mint: build/test verdict is 'fail', not pass"


def test_tests_not_run_ok_only_with_met_criterion():
    rec = _record()
    rec["tests"]["status"] = "not_run"
    assert _refusal(rec) is None
    rec["criterion"] = {"status": "met_partial"}
    assert _refusal(rec) == (
        "review-stamp: refusing to mint: build/test verdict is 'not_run', not pass (exit criterion met_partial)"
    )


def test_unresolved_confinement_foreign_zero_files():
    rec = _record()
    rec["unresolved"] = ["x", "y"]
    assert _refusal(rec) == "review-stamp: refusing to mint: 2 unresolved finding(s)"
    rec = _record()
    rec["confinement_violations"] = 1
    assert _refusal(rec) == "review-stamp: refusing to mint: 1 confinement violation(s)"
    rec = _record()
    rec["prep"]["foreign_claims"] = ["a.py peer", "zzz.py peer"]
    assert _refusal(rec) == "review-stamp: refusing to mint: 1 foreign claim(s) on spine paths: a.py peer"
    rec = _record()
    rec["prep"]["slice_files"] = []
    rec["prep"]["product_files"] = []
    rec["criterion"] = {"status": "not_run"}
    assert _refusal(rec) == (
        "review-stamp: refusing to mint: zero files in the reviewed diff and the exit criterion is not met"
    )


def test_an_empty_diff_mints_on_a_met_criterion():
    rec = _record()
    rec["prep"]["slice_files"] = []
    rec["prep"]["product_files"] = []
    rec["criterion"] = {"status": "met", "observation": "the DR a peer landed answers the ask"}
    assert _refusal(rec) is None


def test_refusal_order_delivery_first():
    rec = _record()
    rec["delivery"]["verdict"] = "FAIL"
    rec["unresolved"] = ["x"]
    assert "delivery verdict" in _refusal(rec)


def test_judge_met_is_agent_delivered():
    verdict, judge = judge_verdict(_record(), all_rows_landed=True, now=NOW)
    assert verdict == "agent-delivered"
    assert judge == {"identity": "execute-review", "observation_summary": "ran and held", "judged_at": NOW}


def test_judge_not_met_and_indeterminate_are_null():
    for status in ("not_met", "indeterminate"):
        rec = _record()
        rec["criterion"]["status"] = status
        assert judge_verdict(rec, all_rows_landed=True, now=NOW) == (None, None)


def test_judge_rows_not_landed_is_null():
    assert judge_verdict(_record(), all_rows_landed=False, now=NOW) == (None, None)


def test_judge_no_record_or_empty_observation_is_null():
    assert judge_verdict(None, all_rows_landed=True, now=NOW) == (None, None)
    rec = copy.deepcopy(_record())
    rec["criterion"]["observation"] = "  "
    assert judge_verdict(rec, all_rows_landed=True, now=NOW) == (None, None)


def test_judge_refused_record_is_null():
    rec = _record()
    rec["unresolved"] = ["x"]
    assert judge_verdict(rec, all_rows_landed=True, now=NOW) == (None, None)


def test_delivery_fail_names_the_unbacked_claims():
    rec = _record()
    rec["delivery"] = {"verdict": "FAIL", "unbacked": [{"claim": "adds retry", "anchor": "no hunk in x.py"}]}
    assert _refusal(rec).endswith("unbacked claims:\n- adds retry [lacked: no hunk in x.py]")


def test_test_verdict_pass_beats_open_lifecycle_status():
    rec = _record()
    rec["tests"] = {"status": "open", "test_verdict": "pass"}
    assert _refusal(rec) is None


def test_test_verdict_fail_refuses_despite_complete_status():
    rec = _record()
    rec["tests"] = {"status": "complete", "test_verdict": "fail"}
    assert _refusal(rec) == "review-stamp: refusing to mint: build/test verdict is 'fail', not pass"


def test_absent_test_verdict_falls_back_to_status():
    rec = _record()
    rec["tests"] = {"status": "pass"}
    assert _refusal(rec) is None
    rec["tests"] = {"status": "open"}
    assert "build/test verdict is 'open'" in _refusal(rec)


def test_criterion_open_ok_lets_an_unmet_criterion_stamp_and_never_a_failed_delivery():
    for status in ("not_met", "indeterminate"):
        rec = _record()
        rec["criterion"]["status"] = status
        rec["tests"]["status"] = "not_run"
        assert mint_refusal(rec, rec["prep"], rec["tests"], criterion_open_ok=True) is None
        assert judge_verdict(rec, all_rows_landed=True, now=NOW) == (None, None)
    rec = _record()
    rec["criterion"]["status"] = "not_met"
    rec["delivery"]["verdict"] = "FAIL"
    assert "delivery verdict is 'FAIL'" in mint_refusal(rec, rec["prep"], rec["tests"], criterion_open_ok=True)
    rec = _record()
    rec["criterion"]["status"] = "not_met"
    rec["tests"]["status"] = "fail"
    assert "build/test verdict is 'fail'" in mint_refusal(rec, rec["prep"], rec["tests"], criterion_open_ok=True)


# --- baseline-equal test failures -------------------------------------------------------------

import pytest  # noqa: E402

from coordinator_core.completion_receipts.verdict import baseline_clears, effective_test_verdict  # noqa: E402


def _baseline_record(**over):
    record = {
        "test_verdict": "fail",
        "tests_failed": 12,
        "baseline_method": "export",
        "baseline": {"pre_existing": [f"t{i}" for i in range(12)], "caused": [], "unverified": []},
    }
    record.update(over)
    return record


def test_failures_identical_at_baseline_read_as_pass():
    assert baseline_clears(_baseline_record()) and effective_test_verdict(_baseline_record()) == "pass"


def test_baseline_equal_tests_do_not_block_the_mint():
    integration = {"delivery": {"verdict": "PASS"}, "criterion": {"status": "met"}}
    prep = {"slice_files": ["a.py"], "product_files": ["a.py"]}
    assert mint_refusal(integration, prep, _baseline_record()) is None


@pytest.mark.parametrize(
    "over",
    [
        {"baseline": {"pre_existing": ["t0"], "caused": ["t1"], "unverified": []}},
        {"baseline": {"pre_existing": ["t0"], "caused": [], "unverified": ["t1"]}},
        {"tests_failed": 13},
        {"baseline_method": "rerun"},
        {"baseline": {"pre_existing": [], "caused": [], "unverified": []}},
    ],
)
def test_a_caused_unverified_or_unattributed_failure_still_refuses(over):
    record = _baseline_record(**over)
    assert not baseline_clears(record) and effective_test_verdict(record) == "fail"


def test_all_foreign_failures_clear():
    record = _baseline_record(
        tests_failed=2, baseline={"pre_existing": [], "foreign": ["a", "b"], "caused": [], "unverified": []}
    )
    assert baseline_clears(record) and effective_test_verdict(record) == "pass"


def test_pre_existing_plus_foreign_mix_covering_the_failures_clears():
    record = _baseline_record(
        tests_failed=2, baseline={"pre_existing": ["a"], "foreign": ["b"], "caused": [], "unverified": []}
    )
    assert baseline_clears(record)


def test_foreign_plus_one_caused_does_not_clear():
    record = _baseline_record(
        tests_failed=2, baseline={"pre_existing": [], "foreign": ["a"], "caused": ["b"], "unverified": []}
    )
    assert not baseline_clears(record)


def test_foreign_under_a_heuristic_method_does_not_clear():
    record = _baseline_record(
        tests_failed=1, baseline_method="heuristic",
        baseline={"pre_existing": [], "foreign": ["a"], "caused": [], "unverified": []},
    )
    assert not baseline_clears(record)


def _no_delivery() -> dict:
    rec = _record()
    rec["delivery"] = {}
    return rec


def test_absent_delivery_mints_when_the_tier_exempts_the_verifier():
    rec = _no_delivery()
    assert mint_refusal(rec, rec["prep"], rec["tests"], delivery_exempt=True) is None


def test_absent_delivery_refuses_when_the_tier_does_not_exempt_it():
    rec = _no_delivery()
    assert "delivery verdict is None" in mint_refusal(rec, rec["prep"], rec["tests"], delivery_exempt=False)
    assert "delivery verdict is None" in _refusal(rec)


def test_a_present_fail_delivery_refuses_even_when_exempt():
    rec = _record()
    rec["delivery"] = {"verdict": "FAIL"}
    assert "delivery verdict is 'FAIL'" in mint_refusal(rec, rec["prep"], rec["tests"], delivery_exempt=True)
