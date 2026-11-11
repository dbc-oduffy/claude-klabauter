"""dispatch.land_run on an un-rerun repo-wide invariant: the refusal hands the run a retest step (the
landing spawns nothing), a fresh passing record lands it, a failing one past the attempt budget refuses
naming the invariant node, and the emitted commit stage runs the retest and re-invokes the landing."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, land_run, landing_retest
from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp
from coordinator_core.ops.dispatch_emit import landing_targeted_tests as ltt
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


def test_no_retest_verdict_past_the_budget_refuses_naming_the_retest_remedy_and_measures_nothing(owed, monkeypatch):
    def measured(*a, **k):
        raise AssertionError("the no-verdict branch has no record to read a measurement from")

    monkeypatch.setattr(ltt, "owed_check_cleared", measured)
    head = _git(["rev-parse", "HEAD"], owed).strip()

    out = _land(owed, retest_attempts=MAX_IN_RUN_REMEDY_ATTEMPTS)

    assert out["committed"] is False and out["refused"] == "invariant-unmeasured", out
    details = [f["detail"] for f in out["integrity"] if INV in f["detail"]]
    assert details and all("no fresh retest verdict exists" in d and "re-run: " in d for d in details), details
    assert _git(["rev-parse", "HEAD"], owed).strip() == head


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


def _base_red_record(root, **extra):
    _record(
        root, "fail", tests_failed=1, failed_tests=[INV],
        baseline={"method": "isolated-export", "caused": [], "pre_existing": [], "unverified": [],
                  "foreign": [INV], "evidence": [INV]},
        **extra,
    )


def test_an_invariant_red_at_base_lands_as_pre_existing_not_a_refusal(owed):
    _base_red_record(owed)

    out = _land(owed, retest_attempts=MAX_IN_RUN_REMEDY_ATTEMPTS)

    assert out["committed"] is True, out
    assert "a.py" in _git(["show", "--name-only", "--format=", "HEAD"], owed).split()


def test_an_invariant_red_in_the_export_only_still_refuses(owed):
    _record(
        owed, "fail", tests_failed=1, failed_tests=[INV],
        baseline={"method": "isolated-export", "caused": [INV], "pre_existing": [], "unverified": [],
                  "foreign": [], "evidence": []},
    )

    out = _land(owed, retest_attempts=MAX_IN_RUN_REMEDY_ATTEMPTS)

    assert out["committed"] is False and out["refused"] == "targeted-tests-failed", out


def test_a_testless_retest_attempt_never_replaces_a_measured_verdict(owed, monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import retest_targeted

    _base_red_record(owed)
    path = landing_retest.fresh_verdict_path(owed, RUN)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(retest_targeted, "_with_invariants", lambda root, tests, *a, **k: tests)
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"run_id": RUN, "script_path": ""}), encoding="utf-8")

    rc = retest_targeted.main(["--params-file", str(params), "--repo-root", str(owed)], dirty=lambda r: [])

    assert rc == 0 and path.read_text(encoding="utf-8") == before


def test_a_testless_retest_attempt_runs_the_owed_invariants_instead_of_writing_stale(owed, monkeypatch, tmp_path):
    from coordinator_core.ops.dispatch_emit import landing_integrity, retest_targeted

    monkeypatch.setattr(landing_integrity, "landing_invariant_nodes", lambda root, paths, rid: [INV])
    params = tmp_path / "params.json"
    params.write_text(json.dumps({"run_id": RUN, "script_path": ""}), encoding="utf-8")
    spawned: list = []
    inv_file = owed / INV.split("::")[0]
    inv_file.parent.mkdir(parents=True, exist_ok=True)
    inv_file.write_text("def test_x():\n    pass\n", encoding="utf-8")

    rc = retest_targeted.main(
        ["--params-file", str(params), "--repo-root", str(owed)],
        dirty=lambda r: ["a.py"], spawn=lambda *a, **k: spawned.append(a) or True,
    )

    assert rc == 0 and spawned, spawned
    record = landing_retest._retest_record(owed, RUN)
    assert record is not None and record["status"] == landing_retest.RETEST_RUNNING
    assert INV in record["tests"]


def test_an_isolated_record_covers_only_the_export_inputs_so_a_peer_edit_never_stales_it(owed):
    from coordinator_core.git.git_state import head_sha
    from coordinator_core.ops.dispatch_emit import retest_targeted

    test_file = INV.split("::")[0]
    path = landing_retest.fresh_verdict_path(owed, RUN)
    meta = {"root": str(owed / "export"), "write_set": ["a.py"], "files": ["a.py"]}
    retest_targeted._write_dual(
        owed, path, path.with_suffix(".log"), path.with_suffix(".export.log"), RUN, [test_file], [], "", meta,
        lambda r: [], head_sha, ("pass", "", [0], False), ("pass", "", [0], False), None,
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    (owed / "peer_dep.py").write_text("x = 2\n", encoding="utf-8")

    assert list(record["covered"]) == ["a.py"]
    assert landing_retest.fresh_targeted_verdict(owed, RUN) is not None
