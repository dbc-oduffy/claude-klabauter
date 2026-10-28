"""A landing refusing ``targeted-tests-failed`` names the in-run retest step (only the failing nodes,
bounded attempts) and every refusal names its test or says none was recorded."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_targeted_tests import (  # noqa: F401
    _fire,
    _git,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

INVARIANT = "coordinator_core/tests/test_machinery_paths.py::test_every_tracked_state_first_segment_is_on_the_allowlist"
CAUSED = {
    "status": "fail", "tests_failed": 1,
    "baseline": {"caused": [INVARIANT], "pre_existing": [], "unverified": []},
}


def test_refusal_carries_a_retest_step_on_exactly_the_failing_node(repo, monkeypatch):
    import subprocess

    real = subprocess.Popen
    argvs: list = []

    def spy(*a, **k):
        argvs.append(" ".join(map(str, a[0])) if a and isinstance(a[0], (list, tuple)) else str(a))
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    head = _write(repo)

    out = _fire(repo, "ok.py", targeted_tests=CAUSED)

    assert not [a for a in argvs if " -m pytest" in a or a.split()[0].endswith("pytest")]

    assert out["committed"] is False and out["refused"] == ltt.TARGETED_TESTS_FAILED
    step = out["remedy_step"]
    assert step["nodes"] == [INVARIANT]
    assert step["attempt"] == 1 and step["max_attempts"] == MAX_IN_RUN_REMEDY_ATTEMPTS
    assert step["exhausted"] is False
    assert f"--tests {INVARIANT.split('::')[0]}" in step["command"]
    assert out["targeted_tests_attempts"] == 0
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_refusal_after_the_attempt_bound_is_final_and_names_the_node(repo):
    _write(repo)

    out = _fire(repo, "ok.py", targeted_tests=CAUSED, retest_attempts=MAX_IN_RUN_REMEDY_ATTEMPTS)

    assert out["committed"] is False
    assert out["remedy_step"]["exhausted"] is True
    detail = out["integrity"][0]["detail"]
    assert INVARIANT in detail and "in-run retest spent" in detail
    assert out["targeted_tests_attempts"] == MAX_IN_RUN_REMEDY_ATTEMPTS


def test_a_passing_verdict_carries_no_remedy_step(repo):
    _write(repo)

    out = _fire(repo, "ok.py", targeted_tests={"status": "pass", "tests_failed": 0, "baseline": None})

    assert out["committed"] is True, out
    assert "remedy_step" not in out


@pytest.mark.parametrize("verdict, expect", [
    ({"status": "fail", "tests_failed": 3, "baseline": None}, ltt.NO_NODE_RECORDED),
    ({"status": "fail", "tests_failed": 3, "baseline": None, "failed_tests": ["t/test_a.py::test_x"]},
     "t/test_a.py::test_x"),
    ({"status": "error", "tests_failed": 0, "baseline": None}, ltt.NO_NODE_RECORDED),
    ({"status": "fail", "tests_failed": 2, "baseline": {"caused": [], "pre_existing": [], "unverified": []}},
     ltt.NO_NODE_RECORDED),
])
def test_unattributed_findings_name_a_node_or_say_none_recorded(verdict, expect):
    findings, check = ltt.failed_targeted_tests(verdict)

    assert check == "failed" and findings
    assert all(f.path == "" and expect in f.detail for f in findings)


def test_nodes_argument_names_the_failures_of_an_unattributed_verdict():
    findings, _ = ltt.failed_targeted_tests(
        {"status": "fail", "tests_failed": 1, "baseline": None}, nodes=["a/test_b.py::test_c"]
    )

    assert "a/test_b.py::test_c" in findings[0].detail
    assert ltt.NO_NODE_RECORDED not in findings[0].detail


def test_retest_step_is_none_without_a_targeted_failure():
    assert ltt.retest_step([ltt.Finding("x.py", "import-closure-untracked", "d")], {}) is None


def test_retest_step_parses_the_repo_wide_invariant_node_and_falls_back_to_the_file():
    inv = ltt.Finding(
        "coordinator_core/tests/test_machinery_paths.py", ltt.TARGETED_TESTS_FAILED,
        f"repo-wide invariant {INVARIANT} measures a path this landing touches; re-run: x",
    )
    bare = ltt.Finding("pkg/test_z.py", ltt.TARGETED_TESTS_FAILED, "no node here")

    assert ltt.retest_step([inv], {})["nodes"] == [INVARIANT]
    assert ltt.retest_step([bare], {})["nodes"] == ["pkg/test_z.py"]
