"""The test-runner prompts run only covering tests, in the foreground, and leave no process behind."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, emit

from .test_ask_compose import _JUDGE_KW, _compose

_RULE = emit._SCOPED_FOREGROUND_CLAUSE


def test_the_shared_clause_scopes_foregrounds_and_reaps():
    clause = emit._test_runner_clause()
    assert _RULE in clause
    assert "only the test files covering the touched files" in clause
    assert "never a broad suite" in clause
    assert "in the foreground" in clause
    assert "every process you started has exited" in clause


def test_the_halt_reverify_prompt_carries_the_rule():
    js = ask_compose._review_recover_js(
        head="H", anchor="A", session_tail="", record_dir="state/warp/r1", run_id="r1", repo_root="REPO"
    )
    assert emit._test_runner_clause("REPO") in js


def test_the_verify_tests_step_no_longer_backgrounds_the_run():
    call = ask_compose._verify_call(
        head="H", anchor="A", test_js="'T'", docs_parts=(), verdict_parts=(), judged=False,
        schema={}, run_id="r1",
    )
    assert "(1) TESTS. Run the scoped tests first, in the foreground" in call
    assert "background" not in call


def test_the_composed_judged_script_never_backgrounds_the_tests():
    assert "run in the background" not in _compose(**_JUDGE_KW)
