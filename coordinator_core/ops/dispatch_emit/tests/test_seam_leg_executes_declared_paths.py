"""The seam leg relays `review.reachability` over the landed range; an unreachable path fails the
plan that wrote it closed."""

from __future__ import annotations

import json

from .test_seam_wave_boundary import (
    CLEAN_REPLY, P1, P2, _NODE, _compose, _run_script, _two_plan_waves,
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
    from .test_seam_wave_boundary import _row

    return [
        [_row("P1-C1", ["a.py", "tests/test_a.py", "tests/data.json"], P1)],
        [_row("P2-C1", ["b.py"], P2, reads=["a.py"])],
    ]


def test_landed_python_test_files_ride_into_the_leg_behind_a_non_empty_guard():
    leg = _seam_prompt(_compose(_test_row_waves()))
    assert "const tests = [...new Set(_seamCommitted.flatMap((i) => _landed[i].declared))].filter(" in leg
    assert "const tests = _paths.filter(" not in leg
    assert "python -m pytest <tests> -q" in leg
    assert "tests.length ?" in leg


@_NODE
def test_an_execution_failed_finding_halts_the_plan_that_wrote_the_failing_file(tmp_path):
    reply = {"verdict": "DRIFT", "per_plan": {}, "findings": [{"class": "execution-failed", "path": "tests/test_a.py", "blocking": True}]}
    out = _run_script(tmp_path, _compose(_test_row_waves()), reply)
    assert P1 in out["haltedPlans"] and "work:P2-C1" not in out["calls"]
    prompt = out["seamParams"][0]
    listed = json.loads(prompt.split("Tests: ", 1)[1].split(". Step 3", 1)[0])
    assert "tests/test_a.py" in listed
    assert all(p.endswith(".py") and "test_" in p for p in listed)


@_NODE
def test_a_leg_with_no_landed_test_file_carries_no_tests_step(tmp_path):
    out = _run_script(tmp_path, _compose(_two_plan_waves()), CLEAN_REPLY)
    assert "Tests: " not in out["seamParams"][0]
    assert "Step 3" not in out["seamParams"][0]


def test_the_leg_prompt_sends_a_saved_reply_under_scratch_never_the_repo_root():
    leg = _seam_prompt(_compose(_two_plan_waves()))
    assert "`scratch/seam/<run-id>/`" in leg
    assert "never the repo root" in leg
