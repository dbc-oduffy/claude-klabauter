"""An indeterminate review re-verify stays on the reply as an advisory (test evidence never refuses a
landing); a fresh retest that ran the targeted set clears it, one that ran no test does not."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li, terminal_commit
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _fire,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TEST_FILE = "tests/test_mod.py"
NODE = f"{TEST_FILE}::test_x"
RUN = "run-1"


def _land(repo) -> None:
    (repo / "tests").mkdir()
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / TEST_FILE).write_text("import mod\n\n\ndef test_x():\n    assert mod.x\n", encoding="utf-8")


def _review(reverify):
    return {**reviewed(), "tests": {"status": "pass"}, li.REVERIFY_KEY: reverify}


def _indeterminate():
    return {"status_before": "indeterminate", "status_after": None, "failing": [], "reverted": []}


def _write_retest(repo, tests, **extra) -> None:
    path = li.fresh_verdict_path(repo, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": RUN, "head": _git(["rev-parse", "HEAD"], repo), "tests": tests,
        "covered": li.landing_fingerprint(repo, ["mod.py", TEST_FILE]), "status": "pass", **extra,
    }), encoding="utf-8")


def _gate(repo, targeted):
    params = {"targeted_tests": targeted, "inline_review": _review(_indeterminate())}
    return terminal_commit._landing_integrity_gate(repo, ["mod.py"], [], None, params, RUN)


def test_indeterminate_reverify_with_no_tests_run_lands_with_the_advisory(repo):
    _land(repo)

    out = _fire(repo, "mod.py", inline_review=_review(_indeterminate()))

    assert out["committed"] is True
    assert out["advisory"][0]["reason"] == li.REVIEW_REVERIFY_INDETERMINATE
    assert "re-verify did not run" in out["advisory"][0]["detail"]


def test_retest_that_ran_no_tests_does_not_clear_an_indeterminate_reverify(repo):
    _land(repo)
    _write_retest(repo, [])

    out = _gate(repo, {"status": "fail", "tests_failed": 1, "baseline": None})

    assert "refused" not in out
    assert out["advisory"][0]["reason"] == li.REVIEW_REVERIFY_INDETERMINATE


def test_retest_that_ran_the_targeted_set_still_clears_an_indeterminate_reverify(repo):
    _land(repo)
    _write_retest(repo, [NODE])

    out = _gate(repo, {"status": "fail", "tests_failed": 1, "baseline": None,
                       "failed_tests": [NODE]})

    assert out["targeted_tests_check"] == "pass-on-retest"
    assert "refused" not in out
