"""The targeted-tests leg counts node ids the run's recorded review.reverify sidecar (reverify.json,
reached through inline_review.reverify.record) names red at base as pre-existing; a red absent from it
still refuses."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_retest as lr
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt

RUN = "r1"
RECORD_REL = "share/slice.pre-review/reverify.json"
NODE = "pkg/tests/test_scan.py::test_real_tree_passes"
OTHER = "pkg/tests/test_other.py::test_other"


@pytest.fixture(autouse=True)
def _fixed_head(monkeypatch):
    from coordinator_core.git import git_state

    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: "h1")


def _sidecar(root, **fields) -> None:
    path = root / RECORD_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def _params() -> dict:
    return {"inline_review": {ltt.REVERIFY_KEY: {"status_after": "pass", "record": RECORD_REL}}}


def _fresh(root, tests, unverified) -> None:
    path = lr.fresh_verdict_path(root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    baseline = {"caused": [], "pre_existing": [], "foreign": [], "unverified": list(unverified)}
    path.write_text(json.dumps({
        "run_id": RUN, "head": "h1", "covered": {}, "status": "fail", "tests": tests,
        "tests_run": 3, "tests_failed": 1, "baseline": baseline,
    }), encoding="utf-8")


def test_sidecar_failing_at_base_clears_a_fresh_red_the_retest_did_not_run(tmp_path):
    _sidecar(tmp_path, failing_at_base=[NODE])
    _fresh(tmp_path, ["pkg/tests/test_ran.py"], [NODE])

    findings, check = li.landing_test_gate(tmp_path, _params(), RUN)

    assert list(findings) == [] and check == "pre-existing-only"
    assert findings.foreign_evidence == {NODE: [li.FAILING_AT_BASE]}


def test_sidecar_pre_existing_clears_a_recorded_fail(tmp_path):
    _sidecar(tmp_path, pre_existing=[NODE])
    lr.fresh_verdict_path(tmp_path, RUN).parent.mkdir(parents=True, exist_ok=True)
    lr.fresh_verdict_path(tmp_path, RUN).write_text(json.dumps(
        {"run_id": RUN, "status": "fail", "tests": [], "export": {"status": "fail", "failed_tests": [NODE]}}),
        encoding="utf-8")
    params = {**_params(), "targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": None}}

    findings, check = li.landing_test_gate(tmp_path, params, RUN)

    assert list(findings) == [] and check == "pre-existing-only"
    assert findings.foreign_evidence == {NODE: [li.FAILING_AT_BASE]}


def test_red_absent_from_the_sidecar_still_refuses(tmp_path):
    _sidecar(tmp_path, failing_at_base=[OTHER])
    _fresh(tmp_path, ["pkg/tests/test_ran.py"], [NODE])

    findings, check = li.landing_test_gate(tmp_path, _params(), RUN)

    assert check == "failed" and NODE in findings[0].detail


def test_sidecar_naming_nothing_changes_nothing(tmp_path):
    _sidecar(tmp_path)
    _fresh(tmp_path, ["pkg/tests/test_ran.py"], [NODE])

    findings, check = li.landing_test_gate(tmp_path, _params(), RUN)

    assert check == "failed"


def test_record_outside_the_worktree_is_ignored(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "reverify.json").write_text(json.dumps({"failing_at_base": [NODE]}), encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    params = {"inline_review": {ltt.REVERIFY_KEY: {"record": "../outside/reverify.json"}}}

    assert ltt.sidecar_base_red(root, params) == set()
    assert ltt.sidecar_base_red(root, {}) == set()
