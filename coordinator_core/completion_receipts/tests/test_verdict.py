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
