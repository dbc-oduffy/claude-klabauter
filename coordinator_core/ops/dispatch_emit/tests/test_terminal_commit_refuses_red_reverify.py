"""dispatch.terminal_commit refuses a clean commit when the run's review re-verify ends red on a
test file the landing touches (the f4b775 shape)."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li, terminal_commit
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit import landing_targeted_tests
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


def _land(repo) -> str:
    (repo / "tests").mkdir()
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / TEST_FILE).write_text("import mod\n\n\ndef test_x():\n    assert mod.x\n", encoding="utf-8")
    return _git(["rev-parse", "HEAD"], repo)


def _reverify(after, before="fail", failing=(NODE,)):
    return {"status_before": before, "status_after": after, "failing": list(failing), "reverted": []}


def _review(reverify):
    return {**reviewed(),
            "tests": {"status": "pass"}, landing_targeted_tests.REVERIFY_KEY: reverify}


def test_red_reverify_on_a_landed_importer_is_refused_naming_the_test_file(repo):
    head = _land(repo)

    out = _fire(repo, "mod.py", inline_review=_review(_reverify("fail")))

    assert out["committed"] is False
    assert out["refused"] == landing_targeted_tests.REVIEW_REVERIFY_FAILED
    assert out["integrity"][0]["path"] == TEST_FILE
    assert out["reverify_check"] == "fail"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_red_reverify_from_a_subdirectory_suite_is_refused(repo):
    (repo / "lab" / "tests").mkdir(parents=True)
    (repo / "lab" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "lab" / TEST_FILE).write_text("import mod\n\n\ndef test_x():\n    assert mod.x\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "lab/mod.py", inline_review=_review(_reverify("fail")))

    assert out["committed"] is False
    assert out["refused"] == landing_targeted_tests.REVIEW_REVERIFY_FAILED
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_green_after_revert_commits(repo):
    _land(repo)

    out = _fire(repo, "mod.py", inline_review=_review(_reverify("pass")))

    assert out["committed"] is True, out
    assert out["reverify_check"] == "pass"


def test_null_reverify_commits_and_reports_absent(repo):
    _land(repo)

    out = _fire(repo, "mod.py", inline_review=_review(None))

    assert out["committed"] is True, out
    assert out["reverify_check"] == "absent"


def test_red_on_a_test_importing_nothing_landed_commits_off_landing(repo):
    _land(repo)
    (repo / "tests" / "test_other.py").write_text("def test_y():\n    pass\n", encoding="utf-8")

    out = _fire(repo, "mod.py", inline_review=_review(_reverify("fail", failing=("tests/test_other.py::test_y",))))

    assert out["committed"] is True, out
    assert out["reverify_check"] == "fail-off-landing"


def _gate_with_retest(repo, retest_tests):
    _land(repo)
    path = landing_retest.fresh_verdict_path(repo, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": RUN, "head": _git(["rev-parse", "HEAD"], repo), "tests": retest_tests,
        "covered": landing_retest.landing_fingerprint(repo, ["mod.py", TEST_FILE]), "status": "pass",
    }), encoding="utf-8")
    params = {
        "targeted_tests": {"status": "fail", "tests_failed": 1, "baseline": None},
        "inline_review": _review(_reverify("fail")),
    }
    return terminal_commit._landing_integrity_gate(repo, ["mod.py"], [], None, params, RUN)


def test_retest_that_ran_the_red_file_clears_the_refusal(repo):
    out = _gate_with_retest(repo, [NODE])

    assert out["targeted_tests_check"] == "pass-on-retest"
    assert "refused" not in out


def test_retest_that_never_ran_the_red_file_does_not_clear_it(repo):
    out = _gate_with_retest(repo, ["tests/test_elsewhere.py::test_z"])

    assert out["refused"] == landing_targeted_tests.REVIEW_REVERIFY_FAILED


def test_retest_record_naming_no_files_does_not_clear_it(repo):
    out = _gate_with_retest(repo, [])

    assert out["refused"] == landing_targeted_tests.REVIEW_REVERIFY_FAILED
