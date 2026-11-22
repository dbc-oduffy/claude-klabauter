"""A targeted-tests red naming no failing node degrades to a retest-clearable advisory at the landing's
default entry and never refuses; a red naming a node keeps its own kind; the remedy attempt never reads over max."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
from coordinator_core.ops.dispatch_emit.landing_refusal_kinds import is_refusal
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_targeted_tests import (  # noqa: F401
    _fire,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NODE = "pkg/tests/test_a.py::test_x"
NO_NODE_VERDICTS = [
    {"status": "fail", "tests_failed": 1, "baseline": None},
    {"status": "error", "tests_failed": 1, "baseline": None},
    {"status": "fail", "tests_failed": 1, "baseline": {"caused": [], "pre_existing": [], "unverified": []}},
    {"status": "error", "tests_failed": 0, "baseline": None, "tests_run": 5},
]


@pytest.mark.parametrize("verdict", NO_NODE_VERDICTS)
@pytest.mark.parametrize("attempts", [0, MAX_IN_RUN_REMEDY_ATTEMPTS, MAX_IN_RUN_REMEDY_ATTEMPTS + 1])
def test_a_red_naming_no_node_lands_with_an_advisory_naming_the_omission(repo, verdict, attempts):
    _write(repo)

    out = _fire(repo, "ok.py", targeted_tests=verdict, retest_attempts=attempts)

    assert out["committed"] is True and not out.get("refused"), out
    assert "remedy_step" not in out
    assert [a["reason"] for a in out["advisory"]] == [ltt.TARGETED_TESTS_FAILED]
    assert ltt.NO_NODE_RECORDED in out["advisory"][0]["detail"]


@pytest.mark.parametrize("verdict", NO_NODE_VERDICTS)
def test_a_red_naming_no_node_is_test_evidence_that_never_refuses(verdict):
    findings, check = ltt.failed_targeted_tests(verdict)

    assert check == "failed" and len(findings) == 1 and findings[0].path == ""
    assert findings[0].reason in ltt.TEST_EVIDENCE_REASONS and not is_refusal(findings[0].reason)
    refusing, advisory = ltt.split_test_advisory(findings)
    assert refusing == [] and advisory == findings
    assert ltt.NO_NODE_RECORDED in findings[0].detail


@pytest.mark.parametrize("verdict", [
    {"status": "fail", "tests_failed": 1, "baseline": None, "failed_tests": [NODE]},
    {"status": "fail", "tests_failed": 1, "baseline": {"caused": [NODE]}},
])
def test_a_red_naming_a_node_keeps_its_kind_and_the_node(verdict):
    findings, _ = ltt.failed_targeted_tests(verdict)

    assert {f.reason for f in findings} == {ltt.TARGETED_TESTS_FAILED}
    assert NODE in findings[0].detail


def test_a_red_naming_a_node_lands_with_an_advisory_naming_it(repo):
    _write(repo)
    verdict = {"status": "fail", "tests_failed": 1, "baseline": {"caused": [NODE], "pre_existing": [], "unverified": []}}

    out = _fire(repo, "ok.py", targeted_tests=verdict, retest_attempts=MAX_IN_RUN_REMEDY_ATTEMPTS)

    assert out["committed"] is True and not out.get("refused"), out
    assert [a["reason"] for a in out["advisory"]] == [ltt.TARGETED_TESTS_FAILED]
    assert NODE in out["advisory"][0]["detail"]


def _wave_sidecar(repo, verdict: str, fix_file: str, after: str) -> str:
    rel = "state/wave-review.md"
    (repo / "state").mkdir(exist_ok=True)
    rows = (
        '[{"id": "F1", "status": "applied", "verified": true, "file": "%s", "before": "x = 0", "after": "%s"}]'
        % (fix_file, after)
    )
    (repo / rel).write_text(
        f"---\nverdict: {verdict}\n---\n\n## Findings Ledger\n\n```json\n{rows}\n```\n", encoding="utf-8"
    )
    return rel


def _review_with_wave(repo, rel: str) -> dict:
    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_targeted_tests import _reviewed

    return {**_reviewed(repo), "wave_sidecar_paths": [rel]}


def test_a_standing_requires_changes_review_with_owed_findings_refuses_the_commit(repo):
    head = _write(repo)
    rel = _wave_sidecar(repo, "REQUIRES_CHANGES", "ok.py", "x = 999")

    out = _fire(repo, "ok.py", inline_review=_review_with_wave(repo, rel))

    assert out["committed"] is False and out["refused"] == "review-owed", out
    assert "F1" in out["error"] and "REQUIRES_CHANGES" in out["error"]
    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_targeted_tests import _git

    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_same_review_with_its_finding_carried_in_the_tree_commits(repo):
    _write(repo)
    rel = _wave_sidecar(repo, "REQUIRES_CHANGES", "ok.py", "x = 1")

    out = _fire(repo, "ok.py", inline_review=_review_with_wave(repo, rel))

    assert out["committed"] is True and out.get("refused") != "review-owed", out


@pytest.mark.parametrize("done", range(0, MAX_IN_RUN_REMEDY_ATTEMPTS + 4))
def test_the_remedy_attempt_never_reads_over_max_for_any_no_node_red(done):
    findings, _ = ltt.failed_targeted_tests(NO_NODE_VERDICTS[0])

    step = ltt.retest_step(findings, {ltt.RETEST_ATTEMPTS_PARAM: done})

    assert step["attempt"] <= step["max_attempts"] == MAX_IN_RUN_REMEDY_ATTEMPTS
    assert step["kind"] == "no_targeted_tests" and step["exhausted"] is True
    assert step["attempt"] == min(max(done, 1), MAX_IN_RUN_REMEDY_ATTEMPTS)
