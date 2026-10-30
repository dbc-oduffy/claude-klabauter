"""A fresh retest red that the run's records name red at the run base, and that the retest ran or that
reads committed HEAD, is not attributed to the run; a red green at base still refuses."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_retest as lr

RUN = "r1"
TEST_FILE = "pkg/tests/test_drift.py"
NODE = f"{TEST_FILE}::test_real_corpus_no_drift_against_head"
OTHER = "pkg/tests/test_other.py::test_other"


@pytest.fixture(autouse=True)
def _fixed_head(monkeypatch):
    from coordinator_core.git import git_state

    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: "h1")


def _record(root, tests, unverified, caused=()) -> None:
    path = lr.fresh_verdict_path(root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    baseline = {"caused": list(caused), "pre_existing": [], "foreign": [], "unverified": list(unverified)}
    path.write_text(json.dumps({
        "run_id": RUN, "head": "h1", "covered": {}, "status": "fail", "tests": tests, "tests_run": 3, "tests_failed": 1, "baseline": baseline,
    }), encoding="utf-8")


def _params(*base_red) -> dict:
    return {"failing_at_base": list(base_red)} if base_red else {}


def test_base_red_targeted_head_reader_is_not_refused_as_caused(tmp_path):
    _record(tmp_path, [TEST_FILE], [NODE])

    findings, check = li.landing_test_gate(tmp_path, _params(NODE), RUN)

    assert list(findings) == [] and check == "pre-existing-only"


def test_base_red_head_reader_the_retest_did_not_list_is_not_refused(tmp_path):
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / TEST_FILE).write_text("subprocess.run(['git', 'show', 'HEAD:x'])\n", encoding="utf-8")
    _record(tmp_path, ["pkg/tests/test_ran.py"], [NODE])

    findings, check = li.landing_test_gate(tmp_path, _params(NODE), RUN)

    assert list(findings) == [] and check == "pre-existing-only"


def test_red_not_named_base_red_still_refuses(tmp_path):
    _record(tmp_path, [TEST_FILE], [NODE])

    findings, check = li.landing_test_gate(tmp_path, _params(OTHER), RUN)

    assert check == "failed" and NODE in findings[0].detail


def test_base_red_node_the_retest_did_not_run_and_that_reads_no_head_still_refuses(tmp_path):
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    (tmp_path / TEST_FILE).write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    _record(tmp_path, ["pkg/tests/test_ran.py"], [NODE])

    findings, check = li.landing_test_gate(tmp_path, _params(NODE), RUN)

    assert check == "failed" and NODE in findings[0].detail


def test_base_red_node_beside_a_caused_one_still_refuses(tmp_path):
    _record(tmp_path, [TEST_FILE], [NODE], caused=[OTHER])

    findings, check = li.landing_test_gate(tmp_path, _params(NODE, OTHER), RUN)

    assert check == "failed" and any(OTHER in f.detail for f in findings)
