"""A landing refusing ``targeted-tests-failed`` names the in-run retest step (only the failing nodes,
bounded attempts) and every refusal names its test or says none was recorded."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_targeted_tests import (  # noqa: F401
    _fire,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

INVARIANT = "coordinator_core/tests/test_machinery_paths.py::test_every_tracked_state_first_segment_is_on_the_allowlist"
CAUSED = {
    "status": "fail", "tests_failed": 1,
    "baseline": {"caused": [INVARIANT], "pre_existing": [], "unverified": []},
}


@pytest.mark.parametrize("attempts", [0, MAX_IN_RUN_REMEDY_ATTEMPTS])
def test_a_caused_targeted_red_lands_with_an_advisory_naming_the_node(repo, monkeypatch, attempts):
    # Claim model (88252271a8): only LANDING_REFUSAL_KINDS refuse; test evidence is recorded, never refuses.
    import subprocess

    real = subprocess.Popen
    argvs: list = []

    def spy(*a, **k):
        argvs.append(" ".join(map(str, a[0])) if a and isinstance(a[0], (list, tuple)) else str(a))
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    _write(repo)

    out = _fire(repo, "ok.py", targeted_tests=CAUSED, retest_attempts=attempts)

    assert not [a for a in argvs if " -m pytest" in a or a.split()[0].endswith("pytest")]
    assert out["committed"] is True and not out.get("refused"), out
    assert "remedy_step" not in out
    advisory = out["advisory"]
    assert [a["reason"] for a in advisory] == [ltt.TARGETED_TESTS_FAILED]
    assert INVARIANT in advisory[0]["detail"]


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


_INV = "pkg/tests/test_inv.py::test_invariant"


def _spent():
    return {ltt.RETEST_ATTEMPTS_PARAM: MAX_IN_RUN_REMEDY_ATTEMPTS}


def _spent_detail(tmp_path, monkeypatch, fresh):
    from coordinator_core.ops.dispatch_emit import landing_integrity as li

    monkeypatch.setattr(li, "landing_invariant_nodes", lambda *a, **k: [_INV])
    monkeypatch.setattr(li, "fresh_targeted_verdict", lambda *a, **k: fresh)
    (finding,) = li.invariant_findings(tmp_path, ["x.py"], "r1", _spent())
    return finding.detail


def test_spent_retest_with_no_fresh_verdict_says_none_exists(tmp_path, monkeypatch):
    assert "no fresh retest verdict exists" in _spent_detail(tmp_path, monkeypatch, None)


def test_spent_retest_that_passed_without_running_the_invariant_says_so(tmp_path, monkeypatch):
    detail = _spent_detail(tmp_path, monkeypatch, {"status": "pass", "tests": ["pkg/tests/test_other.py"]})
    assert "passed without running it" in detail


@pytest.mark.parametrize("status, tail", [("error", "ended error, measuring no failure"),
                                          (None, "ended without a status, measuring no failure")])
def test_spent_retest_ending_neither_pass_nor_fail_says_no_failure_measured(tmp_path, monkeypatch, status, tail):
    assert tail in _spent_detail(tmp_path, monkeypatch, {"status": status})


def test_spent_retest_failing_the_invariant_itself_says_it_failed(tmp_path, monkeypatch):
    detail = _spent_detail(tmp_path, monkeypatch, {"status": "fail", "baseline": None})
    assert detail.endswith("failed its in-run retest")
