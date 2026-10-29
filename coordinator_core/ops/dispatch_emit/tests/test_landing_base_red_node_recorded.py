"""A recorded targeted-tests fail with no baseline names the failing node from the engine's own
records, and a node the records show red at the run base is attributed failing_at_base, not refused."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_retest as lr
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

RUN = "r1"
NODE = "pkg/tests/test_drift.py::test_real_corpus_no_drift_against_head"
OTHER = "pkg/tests/test_other.py::test_other"
RECORDED_FAIL = {"status": "fail", "tests_failed": 1, "baseline": None}


def _record(root, **fields) -> None:
    path = lr.fresh_verdict_path(root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": RUN, "status": "fail", "tests": ["pkg/tests/test_drift.py"], **fields}),
                    encoding="utf-8")


def _params(**extra) -> dict:
    return {"targeted_tests": dict(RECORDED_FAIL), **extra}


def test_recorded_fail_without_baseline_names_the_node_the_retest_record_holds(tmp_path):
    _record(tmp_path, export={"status": "fail", "failed_tests": [NODE]})

    findings, check = li.landing_test_gate(tmp_path, _params(), RUN)

    assert check == "failed" and findings
    assert all(NODE in f.detail and ltt.NO_NODE_RECORDED not in f.detail for f in findings)
    assert ltt.retest_step(list(findings), {})["nodes"] == [NODE]


def test_live_pass_failures_name_the_node_when_the_export_pass_names_none(tmp_path):
    _record(tmp_path, export={"status": "fail", "failed_tests": []}, live={"status": "fail", "failed_tests": [NODE]})

    findings, _ = li.landing_test_gate(tmp_path, _params(), RUN)

    assert findings and NODE in findings[0].detail and ltt.NO_NODE_RECORDED not in findings[0].detail


def test_no_record_anywhere_still_says_no_node_recorded(tmp_path):
    findings, check = li.landing_test_gate(tmp_path, _params(), RUN)

    assert check == "failed" and ltt.NO_NODE_RECORDED in findings[0].detail


def test_node_red_at_base_is_attributed_failing_at_base_and_does_not_refuse(tmp_path):
    _record(tmp_path, export={"status": "fail", "failed_tests": [NODE]})
    params = _params(inline_review={ltt.REVERIFY_KEY: {"status_after": "pass", "pre_existing": [NODE]}})

    findings, check = li.landing_test_gate(tmp_path, params, RUN)

    assert list(findings) == [] and check == "pre-existing-only"
    assert findings.foreign_evidence == {NODE: [li.FAILING_AT_BASE]}


def test_failing_at_base_param_attributes_the_node(tmp_path):
    _record(tmp_path, export={"status": "fail", "failed_tests": [NODE]})

    findings, check = li.landing_test_gate(tmp_path, _params(failing_at_base=[NODE]), RUN)

    assert list(findings) == [] and check == "pre-existing-only"
    assert findings.foreign_evidence == {NODE: [li.FAILING_AT_BASE]}


def test_node_green_at_base_and_red_after_still_refuses(tmp_path):
    _record(tmp_path, export={"status": "fail", "failed_tests": [NODE]})

    findings, check = li.landing_test_gate(tmp_path, _params(failing_at_base=[OTHER]), RUN)

    assert check == "failed" and NODE in findings[0].detail
    assert not getattr(findings, "foreign_evidence", None)


def test_one_base_red_node_beside_a_caused_one_still_refuses_naming_the_caused(tmp_path):
    _record(tmp_path, export={"status": "fail", "failed_tests": [NODE, OTHER]})

    findings, check = li.landing_test_gate(tmp_path, _params(failing_at_base=[NODE]), RUN)

    assert check == "failed" and any(OTHER in f.detail for f in findings)


def test_base_red_keys_name_the_self_correction_foreign_field():
    from coordinator_core.ops.dispatch_emit.self_correction import FOREIGN_FIELD

    assert FOREIGN_FIELD in ltt.BASE_RED_KEYS
