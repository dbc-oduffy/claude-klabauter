"""dispatch.land_run on an un-rerun repo-wide invariant: the refusal hands the run a retest step (the
landing spawns nothing), a fresh passing record lands it, a failing one past the attempt budget refuses
naming the invariant node, and the emitted commit stage runs the retest and re-invokes the landing."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, land_run, landing_retest
from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp
from coordinator_core.ops.dispatch_emit.halt_remedy_class import MAX_IN_RUN_REMEDY_ATTEMPTS
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import (  # noqa: F401
    RUN,
    _author,
    _declare,
    _git,
    _land,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

INV = "coordinator_core/session/tests/test_machinery_paths.py::test_every_tracked_state_first_segment_is_on_the_allowlist"


@pytest.fixture
def owed(repo, monkeypatch):
    monkeypatch.setattr(imp, "invariants_for_paths", lambda paths, root: [INV])
    monkeypatch.setattr(imp, "existing_invariants", lambda root, nodes: list(nodes))
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    return repo


def _record(root, status, **extra):
    record = {
        "run_id": RUN, "head": _git(["rev-parse", "HEAD"], root).strip(), "status": status,
        "tests": [INV.split("::")[0]], "tests_run": 1, "covered": landing_retest.landing_fingerprint(root, ["a.py"]),
        **extra,
    }
    path = landing_retest.fresh_verdict_path(root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def test_an_unrerun_invariant_refuses_with_a_retest_step_and_spawns_no_pytest(owed, monkeypatch):
    real = subprocess.Popen
    argvs: list = []

    def spy(*a, **k):
        argvs.append(" ".join(map(str, a[0])) if a and isinstance(a[0], (list, tuple)) else str(a))
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    head = _git(["rev-parse", "HEAD"], owed).strip()

    out = _land(owed)

    assert out["committed"] is False and out["refused"] == "targeted-tests-failed", out
    step = out["remedy_step"]
    assert step["nodes"] == [INV] and step["attempt"] == 1 and step["exhausted"] is False
    assert INV in out["integrity"][0]["detail"]
    assert not [a for a in argvs if "pytest" in a]
    assert _git(["rev-parse", "HEAD"], owed).strip() == head


def test_a_fresh_passing_retest_lets_the_reinvoked_landing_commit(owed):
    _record(owed, "pass")

    out = _land(owed, retest_attempts=1)

    assert out["committed"] is True, out
    assert "a.py" in _git(["show", "--name-only", "--format=", "HEAD"], owed).split()


def test_a_failed_retest_past_the_attempt_budget_refuses_naming_the_invariant(owed):
    _record(
        owed, "fail", tests_failed=1, failed_tests=[INV],
        baseline={"caused": [INV], "pre_existing": [], "unverified": []},
    )
    head = _git(["rev-parse", "HEAD"], owed).strip()

    out = _land(owed, retest_attempts=MAX_IN_RUN_REMEDY_ATTEMPTS)

    assert out["committed"] is False and out["refused"] == "targeted-tests-failed", out
    assert out["remedy_step"]["exhausted"] is True
    assert INV in out["error"]
    assert any(INV in f["detail"] and "failed its in-run retest" in f["detail"] for f in out["integrity"])
    assert _git(["rev-parse", "HEAD"], owed).strip() == head


def test_no_retest_verdict_past_the_budget_names_the_missing_verdict_not_a_failure(owed):
    out = _land(owed, retest_attempts=MAX_IN_RUN_REMEDY_ATTEMPTS)

    assert out["committed"] is False and out["refused"] == "targeted-tests-failed", out
    details = [f["detail"] for f in out["integrity"] if INV in f["detail"]]
    assert details and all("no fresh retest verdict exists" in d for d in details), details
    assert not any("failed its in-run retest" in d for d in details)


def test_an_errored_retest_past_the_budget_names_its_status_not_a_failure(owed):
    _record(owed, "error")

    out = _land(owed, retest_attempts=MAX_IN_RUN_REMEDY_ATTEMPTS)

    assert out["committed"] is False, out
    details = [f["detail"] for f in out["integrity"] if INV in f["detail"]]
    assert details and all("ended error" in d for d in details), details
    assert not any("failed its in-run retest" in d for d in details)


def test_the_emitted_commit_stage_runs_the_remedy_step_and_reinvokes_the_landing():
    lines: list = []
    ask_compose._append_docs_and_commit(
        lines, head="h", anchor="a", repo_root="/r", run_id=RUN, manifest_rel="m.json"
    )
    js = "\n".join(lines)

    assert "_commit.remedy_step" in js and "remedy_step.exhausted" in js
    assert "retest-invariant" in js and "retest_targeted" in js and "--poll" in js
    assert js.index("retest-invariant") < js.rindex("_commit = await")
    assert "retest_attempts" in ask_compose._commit_stage_call(head="h", anchor="a", repo_root="/r", run_id=RUN)


def test_every_commit_stage_agent_names_its_model():
    """An agent() with no model: inherits the session's (Opus, ~4x). The published retest-invariant
    call shipped that way; every emitted call names its model."""
    lines: list = []
    ask_compose._append_docs_and_commit(
        lines, head="h", anchor="a", repo_root="/r", run_id=RUN, manifest_rel="m.json"
    )
    js = "\n".join(lines)
    opts = [seg.split("})", 1)[0] for seg in js.split("agentType: ")[1:]]
    assert opts and all("model: '" in o for o in opts), [o[:120] for o in opts if "model: '" not in o]
