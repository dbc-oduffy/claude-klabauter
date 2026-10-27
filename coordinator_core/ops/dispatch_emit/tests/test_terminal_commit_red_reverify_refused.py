"""A red re-verify that names no failing test is refused at the terminal commit, not waved through
as off-landing; a red naming a test off the landing set still commits."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_targeted_tests
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _fire,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _review(reverify):
    return {**reviewed(),
            "tests": {"status": "pass"}, landing_targeted_tests.REVERIFY_KEY: reverify}


def _land(repo) -> None:
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")


@pytest.mark.parametrize("failing", [(), ("",)])
def test_red_reverify_naming_no_failing_test_is_refused(repo, failing):
    _land(repo)
    head = _git(["rev-parse", "HEAD"], repo)
    record = {"status_before": "fail", "status_after": "fail", "failing": list(failing), "reverted": []}

    out = _fire(repo, "mod.py", inline_review=_review(record))

    assert out["committed"] is False
    assert out["refused"] == landing_targeted_tests.REVIEW_REVERIFY_FAILED
    assert out["reverify_check"] == "fail"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_red_reverify_naming_an_unrelated_test_still_commits(repo):
    _land(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_other.py").write_text("def test_y():\n    pass\n", encoding="utf-8")
    record = {"status_before": "fail", "status_after": "fail",
              "failing": ["tests/test_other.py::test_y"], "reverted": []}

    out = _fire(repo, "mod.py", inline_review=_review(record))

    assert out["committed"] is True, out
    assert out["reverify_check"] == "fail-off-landing"
