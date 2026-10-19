"""dispatch.terminal_commit refuses, before commit_v2, a commit whose carried test verdict names a
run-caused failure."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _fire,
    _git,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _write(repo) -> str:
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    return _git(["rev-parse", "HEAD"], repo)


def test_caused_failure_is_refused_naming_the_test_file(repo):
    head = _write(repo)
    verdict = {
        "status": "fail", "tests_failed": 2,
        "baseline": {"caused": ["pkg/tests/test_a.py::test_x"], "pre_existing": [], "unverified": []},
    }

    out = _fire(repo, "ok.py", targeted_tests=verdict)

    assert out["committed"] is False
    assert out["refused"] == landing_integrity.TARGETED_TESTS_FAILED
    assert out["integrity"][0]["path"] == "pkg/tests/test_a.py"
    assert out["targeted_tests_check"] == "failed"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_pass_verdict_commits(repo):
    _write(repo)

    out = _fire(repo, "ok.py", targeted_tests={"status": "pass", "tests_failed": 0, "baseline": None})

    assert out["committed"] is True, out
    assert out["targeted_tests_check"] == "pass"


def test_inline_review_fail_without_targeted_tests_is_refused_unattributed(repo):
    head = _write(repo)
    inline = {"integration_stem": "s", "slices": 1, "fixes": 0, "tests": {"status": "fail", "failed": 3}}

    out = _fire(repo, "ok.py", inline_review=inline)

    assert out["committed"] is False
    assert out["refused"] == landing_integrity.TARGETED_TESTS_FAILED
    assert out["integrity"][0]["path"] == ""
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_absent_verdict_commits_and_reports_no_verdict(repo):
    _write(repo)

    out = _fire(repo, "ok.py")

    assert out["committed"] is True, out
    assert out["targeted_tests_check"] == "no-verdict"
