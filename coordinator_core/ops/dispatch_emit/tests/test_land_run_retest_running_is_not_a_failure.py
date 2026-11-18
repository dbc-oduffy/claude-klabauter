"""dispatch.land_run on a run whose retest record is still ``running``: a live one is a non-refusal await
(no ``targeted-tests-failed`` text, no remedy attempt spent), one past its deadline refuses
``retest-abandoned`` with a runnable re-run, and a red record still refuses naming its node ids."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import (  # noqa: F401
    RUN,
    _author,
    _declare,
    _git,
    _land,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TEST_FILE = "tests/test_a.py"
NODE = f"{TEST_FILE}::test_one"


@pytest.fixture
def run(repo):
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    return repo


def _write_record(root, status, deadline_in=None, **extra):
    record = {
        "run_id": RUN, "head": _git(["rev-parse", "HEAD"], root).strip(), "status": status,
        "tests": [TEST_FILE], "covered": landing_retest.landing_fingerprint(root, ["a.py"]), **extra,
    }
    if deadline_in is not None:
        record["deadline"] = (datetime.now(timezone.utc) + timedelta(seconds=deadline_in)).isoformat()
    path = landing_retest.fresh_verdict_path(root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_a_live_running_retest_is_an_await_not_a_refusal(run):
    _write_record(run, "running", deadline_in=3600)
    head = _git(["rev-parse", "HEAD"], run).strip()

    out = _land(run)

    assert out["committed"] is False and out["refused"] is None and out["remedy_step"] is None, out
    assert out["awaiting"]["awaiting"] == "retest" and out["error"].endswith(" --poll --wait"), out
    assert "targeted-tests-failed" not in out["error"] and "refused" not in out["error"]
    assert _git(["rev-parse", "HEAD"], run).strip() == head


def test_the_await_spends_no_remedy_attempt_on_a_repeat(run):
    _write_record(run, "running", deadline_in=3600)

    first, second = _land(run), _land(run)

    assert first["remedy_step"] is None and second["remedy_step"] is None
    assert first["awaiting"] == second["awaiting"]


def test_a_running_retest_past_its_deadline_refuses_retest_abandoned_with_a_rerun(run):
    _write_record(run, "running", deadline_in=-5)

    out = _land(run)

    assert out["committed"] is False and out["refused"] == "retest-abandoned", out
    assert "abandoned" in out["error"] and f"re-run: {ltt.RETEST_LAUNCHER} --from-task-output" in out["error"]
    assert "awaiting" not in out
    step = out["remedy_step"]
    assert step["nodes"] == [TEST_FILE] and step["attempt"] == 1 and step["exhausted"] is False, step


def test_a_running_record_with_no_deadline_is_abandoned_not_awaited(run):
    _write_record(run, "running")

    out = _land(run)

    assert out["refused"] == "retest-abandoned" and "awaiting" not in out, out


def test_a_red_record_still_refuses_naming_its_nodes(run):
    _write_record(
        run, "fail", failed_tests=[NODE], tests_run=1, tests_failed=1,
        baseline={"caused": [NODE], "pre_existing": [], "unverified": []},
    )

    out = _land(run)

    assert out["committed"] is False and out["refused"] == "targeted-tests-failed", out
    assert "awaiting" not in out and NODE in out["error"] + json.dumps(out["integrity"]), out


def test_retest_step_never_counts_an_await_as_an_attempt():
    wait = li.RetestAwait("", ltt.TARGETED_TESTS_FAILED, "the run's retest is still running")

    assert ltt.retest_step([wait], {}) is None
    assert ltt.retest_step([wait, li.Finding("t.py", ltt.TARGETED_TESTS_FAILED, f"failing: {NODE}")], {})["nodes"] == [NODE]


def test_the_abandoned_reason_is_the_one_constant_and_a_test_evidence_reason():
    assert ltt.RETEST_ABANDONED == landing_retest.RETEST_ABANDONED
    assert ltt.RETEST_ABANDONED in ltt.TEST_EVIDENCE_REASONS
    finding = li.Finding("", ltt.RETEST_ABANDONED, f"abandoned past its deadline; {NODE}; {TEST_FILE}; re-run: cmd")
    assert ltt.remedy_nodes([finding]) == [NODE, TEST_FILE]
