"""The ask verify-tests leg runs test_leg_run detached and awaited on exit; the judge never excuses a zero-test leg."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose


def _tests_call() -> str:
    return ask_compose._verify_tests_call(
        head="H", anchor="A", test_js=ask_compose._test_instruction_js("HEAD", "REPO"), run_id="r1",
        repo_root="REPO", out_path="/run/test-leg.json", base_sha="abc123",
    )


def test_the_leg_runs_test_leg_run_in_the_background_and_reads_its_out_file():
    call = _tests_call()
    assert "coordinator_core.ops.test_leg_run" in call
    assert "--out /run/test-leg.json -- " in call and "--base-sha abc123" in call
    assert "run_in_background: true" in call
    assert "(_manifest.test_targets ?? []).join(' ')" in call
    assert "Read /run/test-leg.json" in call and "verbatim as the test_result" in call


def test_the_leg_carries_no_deadline_clause_and_no_bash_timeout():
    call = _tests_call()
    assert "Deadline:" not in call and "Bash tool timeout" not in call and "timeout:" not in call
    assert "no timeout parameter" in call


def test_no_js_race_bound_wraps_the_verify_tests_call():
    import inspect

    src = inspect.getsource(ask_compose)
    at = src.index("_VERIFY_TEST_RESULT_VAR} = await {verify_tests_call}")
    window = src[at - 400 : at + 400]
    assert "Promise.race" not in window and "VERIFY_WALL_BUDGET_S" not in window and "bounded_js" not in window


def test_the_judge_rules_hold_a_zero_test_leg_not_met_and_keep_stall_only_for_other_legs():
    rules = ask_compose._LEDGER_ITEMS_RULES.replace("\n", " ")
    assert "is inconclusive evidence, never" not in rules
    assert "tests_run 0" in rules and "never stall_only" in rules
    assert "Any other verify leg that stalled or timed out" in rules and "set stall_only true" in rules
