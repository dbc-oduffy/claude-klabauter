"""``failed_targeted_tests`` lands a record C1's writer wrote when every red is foreign (red on pure HEAD, or
``undeclared-dirt``), and still refuses a run-caused, unmeasured or reasonless red."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit.tests.test_retest_isolated_baseline import _setup
from coordinator_core.ops.dispatch_emit.tests.test_retest_node_head_baseline import _base, _dual

RATCHET = "test_a.py::t0"
DIRT = "test_b.py::dirt"


def test_head_red_ratchet_after_skipped_whole_file_lands(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[RATCHET], head_red=[RATCHET], head_ran_short=1)
    assert record["baseline"]["method"] == "head-node-measure"
    assert ltt.failed_targeted_tests(record) == ([], "pre-existing-only")


def test_node_measured_green_at_head_is_caused_and_refuses(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[RATCHET], head_red=[], head_ran_short=1)
    findings, check = ltt.failed_targeted_tests(record)
    assert check == "failed" and [f.path for f in findings] == ["test_a.py"]
    assert findings[0].reason == ltt.TARGETED_TESTS_FAILED


def test_undeclared_dirt_foreign_red_lands(tmp_path):
    record = _dual(tmp_path, export_summary=True, baseline=_base(), live_red=[RATCHET, DIRT])
    assert record["baseline"]["foreign_reason"] == {DIRT: "undeclared-dirt"}
    assert ltt.failed_targeted_tests(record) == ([], "pre-existing-only")


def test_carried_verdict_gets_no_dirt_credit(tmp_path):
    record = _dual(tmp_path, export_summary=True, baseline=_base(), live_red=[RATCHET, DIRT])
    findings, check = ltt.failed_targeted_tests(record, measured=False)
    assert check == "failed" and {f.path for f in findings} == {"test_a.py", "test_b.py"}


def test_foreign_id_without_evidence_or_dirt_reason_refuses():
    baseline = {"caused": [], "foreign": [DIRT], "evidence": [], "foreign_reason": {}}
    findings, check = ltt.failed_targeted_tests({"status": "fail", "tests_failed": 1, "baseline": baseline})
    assert check == "failed" and findings[0].path == "test_b.py"


def test_dirt_reason_on_a_non_foreign_id_is_not_credited():
    baseline = {"caused": [], "pre_existing": [DIRT], "foreign": [], "evidence": [],
                "foreign_reason": {DIRT: "undeclared-dirt"}}
    assert ltt.unevidenced_ids(baseline) == [DIRT]
