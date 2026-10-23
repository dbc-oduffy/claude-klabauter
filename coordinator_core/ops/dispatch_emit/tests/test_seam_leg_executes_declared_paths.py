"""The seam leg relays `review.reachability` over the landed range; an unreachable path fails the
plan that wrote it closed."""

from __future__ import annotations

import json
import subprocess

from coordinator_core.ops.dispatch_emit.emit import compose_script
from .conftest import REVIEW_KW
from .test_seam_wave_boundary import (
    ABSENT_EDGES, CLEAN_REPLY, P1, P2, _NODE, _compose, _row, _run_script, _two_plan_waves,
)


def _seam_prompt(script: str) -> str:
    return script.split("async function _seamLeg", 1)[1].split("  function _seamFail", 1)[0]


def test_the_leg_prompt_relays_reachability_over_the_landed_range():
    leg = _seam_prompt(_compose(_two_plan_waves()))
    assert "review.reachability" in leg
    assert "head_sha: sha" in leg
    assert "paths:" in leg and "_landed[i].paths" in leg


def test_the_leg_prompt_names_what_blocks_and_what_does_not():
    leg = _seam_prompt(_compose(_two_plan_waves()))
    assert "`unreachable`" in leg and "`broken`" in leg
    assert "class: \\'unreachable\\', path, blocking: true" in leg
    assert "never report `undecidable`" in leg
    assert "`op-error`" in leg and "REFUSED" in leg


@_NODE
def test_an_unreachable_finding_without_a_plan_halts_the_plan_that_wrote_its_path(tmp_path):
    reply = {"verdict": "DRIFT", "per_plan": {}, "findings": [{"class": "unreachable", "path": "a.py", "blocking": True}]}
    out = _run_script(tmp_path, _compose(_two_plan_waves()), reply)
    assert P1 in out["haltedPlans"]
    assert "work:P2-C1" not in out["calls"]
    assert out["notStarted"] == ["P2-C1"]
    assert out["drift"][0]["plans"] == [P1] and sorted(out["haltedPlans"]) == [P1, P2]


@_NODE
def test_a_refused_op_error_halts_the_held_plans(tmp_path):
    reply = {"verdict": "REFUSED", "per_plan": {}, "findings": [{"class": "op-error"}]}
    out = _run_script(tmp_path, _compose(_two_plan_waves()), reply)
    assert out["haltedPlans"] == [P2] and out["failures"]


def _test_row_waves():
    return [
        [_row("P1-C1", ["a.py", "tests/test_a.py", "tests/data.json"], P1)],
        [_row("P2-C1", ["b.py"], P2, reads=["a.py"])],
    ]


def test_landed_python_test_files_ride_into_the_leg_behind_a_non_empty_guard():
    script = _compose(_test_row_waves())
    leg = _seam_prompt(script)
    assert "const _seamOwnTests = " in script
    assert "const tests = [...new Set(_seamCommitted.flatMap((i) => (_seamOwnTests[i] || [])))].filter(" in leg
    assert "flatMap((i) => _landed[i].declared))].filter(" not in leg
    assert "const tests = _paths.filter(" not in leg
    assert "python -m pytest <tests> -q" in leg
    assert "tests.length ?" in leg


@_NODE
def test_an_execution_failed_finding_halts_the_plan_that_wrote_the_failing_file(tmp_path):
    reply = {"verdict": "DRIFT", "per_plan": {}, "findings": [{"class": "execution-failed", "path": "tests/test_a.py", "blocking": True}]}
    out = _run_script(tmp_path, _compose(_test_row_waves()), reply)
    assert P1 in out["haltedPlans"] and "work:P2-C1" not in out["calls"]
    assert "seam-fix:wave-1" in out["calls"]
    assert "in-route repair" in out["reasons"][P1]
    prompt = out["seamParams"][0]
    listed = json.loads(prompt.split("Tests: ", 1)[1].split(". Step 3", 1)[0])
    assert "tests/test_a.py" in listed
    assert all(p.endswith(".py") and "test_" in p for p in listed)


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


@_NODE
def test_a_caller_widened_referencer_test_is_declared_but_the_seam_never_runs_it(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "a.py").write_text("x = 1\n")
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (repo / "tests" / "test_ref.py").write_text("from a import x\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    script = compose_script(
        _test_row_waves()[:1] + [[_row("P2-C1", ["b.py"], P2, reads=["a.py"])]],
        name="n", description="d", plan_path="state/mise-inventory/x.spine.md",
        expected_branch="work/x", run_base_sha="a" * 40, seam=ABSENT_EDGES,
        repo_root=repo, **REVIEW_KW,
    )
    declared = script.split("declared: [", 1)[1].split("]", 1)[0]
    assert "'tests/test_ref.py'" in declared
    out = _run_script(tmp_path, script, CLEAN_REPLY)
    listed = json.loads(out["seamParams"][0].split("Tests: ", 1)[1].split(". Step 3", 1)[0])
    assert "tests/test_a.py" in listed
    assert "tests/test_ref.py" not in listed


@_NODE
def test_a_leg_with_no_landed_test_file_carries_no_tests_step(tmp_path):
    out = _run_script(tmp_path, _compose(_two_plan_waves()), CLEAN_REPLY)
    assert "Tests: " not in out["seamParams"][0]
    assert "Step 3" not in out["seamParams"][0]


def test_the_leg_prompt_sends_a_saved_reply_under_scratch_never_the_repo_root():
    leg = _seam_prompt(_compose(_two_plan_waves()))
    assert "`scratch/seam/<run-id>/`" in leg
    assert "never the repo root" in leg
