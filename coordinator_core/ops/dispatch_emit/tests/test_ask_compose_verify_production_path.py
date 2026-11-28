"""ask_compose: the falsifier, judge and verify briefs require driving the production entry point."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_ENTRY_POINT = "drive that production entry point end-to-end"
_FOREIGN_CWD = "from a foreign cwd (not the repo root) where cwd matters"
_FIXTURE_ONLY = "exercises only route-authored test fixtures"
_NOT_PROVING = "does not prove the criterion"
_NO_ENV_DUMP = "Never write the environment (os.environ or a child env) to a file or reply"
_PROBE_SCRATCH = "scratch directory, never the target repo, and are deleted before you return"
_CLAUSES = (_ENTRY_POINT, _FOREIGN_CWD, _FIXTURE_ONLY, _NOT_PROVING, _NO_ENV_DUMP, _PROBE_SCRATCH)


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose() -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO",
        prompt="Prove the ledger write is fsynced through producer_runner.run_all",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=_stub_wrap,
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _assert_clauses(text: str) -> None:
    for clause in _CLAUSES:
        assert clause in text, clause


def test_shared_clause_names_every_requirement() -> None:
    _assert_clauses(ask_compose._PRODUCTION_PATH_CLAUSE)


def test_judge_brief_holds_the_clause() -> None:
    _assert_clauses(ask_compose._JUDGE_PATH_CLAUSE)


def test_falsifier_brief_holds_the_clause() -> None:
    _assert_clauses(ask_compose._FALSIFIER_ACT)


def test_verify_brief_holds_the_clause() -> None:
    _assert_clauses(ask_compose._VERIFY_INTRO)


def test_fixture_only_is_reported_not_met() -> None:
    assert "report it as not proving the criterion, never as met" in ask_compose._PRODUCTION_PATH_CLAUSE


def test_composed_script_carries_the_clause() -> None:
    _assert_clauses(_compose())


_OVERLAY_CMD = "-m coordinator_core.ops.dispatch_emit.clean_overlay_verify --run-id run-1"


def test_default_workflow_runs_the_overlay_producer_as_its_own_leg() -> None:
    text = _compose()
    assert _OVERLAY_CMD in text
    assert f"label: '{ask_compose.OVERLAY_VERIFY_LABEL}'" in text or f'label: "{ask_compose.OVERLAY_VERIFY_LABEL}"' in text


def test_overlay_producer_is_not_in_the_verify_agent_prompt() -> None:
    call = ask_compose._verify_call(
        head="h", anchor="a", test_js=ask_compose._test_instruction_js("HEAD", "REPO"), docs_parts=(),
        verdict_parts=("v",), judged=False, schema={}, run_id="run-1",
    )
    assert "clean_overlay_verify" not in call


def test_overlay_exit_one_folds_into_a_red_test_and_other_exits_do_not() -> None:
    fold = ask_compose._OVERLAY_FOLD_JS
    assert "_ov.exit_code === 1" in fold and "status: 'fail'" in fold
    assert "(!_ov || _ov.exit_code !== 0)" in fold and "log(" in fold
    text = _compose()
    assert text.index("overlay-verify") < text.index("_testResult = _verify.test")


def test_overlay_runs_when_the_tests_leg_ran_even_if_the_verify_result_lacks_test() -> None:
    fold = ask_compose._OVERLAY_FOLD_JS
    head = fold.split("\n", 1)[0]
    assert "_testWanted && _verify && (_verifyTest || _verify.test)" in head
    assert "_verify.test = { ..._t, status: 'fail'" in fold
    assert "const _t = _verify.test ?? _verifyTest ?? {};" in fold
    assert "No module named" not in fold
    assert "_ov.exit_code !== 0" in fold
    assert fold.index("_verify.test = { ..._t") > fold.index("_ov.exit_code === 1")
    text = _compose()
    assert text.index("(_verifyTest || _verify.test)") < text.index("_testResult = _verify.test")


def _fold_with(overlay_js: str, halted: bool = False) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    fold = ask_compose._OVERLAY_FOLD_JS.replace("__OVERLAY__", overlay_js)
    program = (
        "const _errLimitText = () => null; const _USAGE_LIMIT_RE = /zzz/; const _haltOnUsageLimit = () => {}; const log = () => {};\n"
        f"(async () => {{ const _halted = {str(halted).lower()}, _testWanted = true;\n"
        "let _verifyTest = null; let _verify = { test: { status: 'pass', tests_failed: 0, summary: 's' } };\n"
        f"{fold}\nprocess.stdout.write(JSON.stringify(_verify.test)); }})();\n"
    )
    done = subprocess.run([node, "-"], input=program, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _resolved(exit_code, detail) -> str:
    return f"Promise.resolve({json.dumps({'exit_code': exit_code, 'detail': detail})})"


@pytest.mark.parametrize("exit_code", [2, 3, 124])
def test_overlay_non_zero_exit_fails_the_test_naming_the_timeout(exit_code) -> None:
    test = _fold_with(_resolved(exit_code, "overlay verify timed out after 120s: TimeoutExpired"))
    assert test["status"] == "fail" and test["tests_failed"] >= 1
    assert test["summary"].startswith(f"clean-overlay verdict inconclusive (exit {exit_code}): ")
    assert "timed out after 120s" in test["summary"]
    assert test["test_leg"] == {"status": "pass", "run": None, "failed": 0}
    assert test["overlay"]["exit_code"] == exit_code


def test_overlay_thrown_leg_and_null_result_fail_the_test() -> None:
    thrown = _fold_with("Promise.reject(new Error('leg crashed'))")
    assert thrown["status"] == "fail" and "inconclusive (exit none)" in thrown["summary"] and "leg crashed" in thrown["summary"]
    null = _fold_with("Promise.resolve(null)")
    assert null["status"] == "fail" and "inconclusive (exit none)" in null["summary"]


def test_overlay_green_and_halted_runs_still_pass() -> None:
    assert _fold_with(_resolved(0, "overlay verdict: green"))["status"] == "pass"
    assert _fold_with("Promise.resolve(null)", halted=True)["status"] == "pass"


def test_overlay_interpreter_import_fault_fails_closed_never_passes_open() -> None:
    test = _fold_with(_resolved(1, "/usr/bin/python: No module named coordinator_core"))
    assert test["status"] == "fail" and test["tests_failed"] >= 1
    assert "No module named coordinator_core" in test["summary"]


def test_verify_tests_leg_builds_and_runs_in_the_clean_export() -> None:
    directive = ask_compose.engine_module_command(
        ask_compose._OVERLAY_VERIFY_MODULE, "REPO", "--export-run --run-id run-1 --repo-root REPO --"
    )
    leg = ask_compose._verify_tests_call(
        head="h", anchor="a", run_id="run-1", repo_root="REPO", out_path="o.json",
        test_js=ask_compose._test_instruction_js("HEAD", "REPO", export_command=directive),
    )
    assert directive in leg
    plain = ask_compose._verify_tests_call(
        head="h", anchor="a", run_id="run-1", repo_root="REPO", out_path="o.json",
        test_js=ask_compose._test_instruction_js("HEAD", "REPO"),
    )
    assert directive not in plain
    assert directive in _compose()


def test_verify_agent_prompt_does_not_run_the_export() -> None:
    call = ask_compose._verify_call(
        head="h", anchor="a", test_js=ask_compose._test_instruction_js("HEAD", "REPO"), docs_parts=(),
        verdict_parts=("v",), judged=False, schema={}, run_id="run-1", test_result_var="_verifyTest",
    )
    assert "--export-run" not in call


def test_commit_halt_reason_carries_refusal_and_error_text() -> None:
    lines = "\n".join(
        ask_compose.roadmap_commit_lines(head="h", anchor="a", repo_root=str(Path(__file__).resolve().parents[4]), run_id="2026-11-13-x-aaaaaa")
    )
    assert "reason: " + ask_compose._COMMIT_HALT_REASON_JS in lines
    assert "_commit?.refused, _commit?.error" in ask_compose._COMMIT_HALT_REASON_JS
