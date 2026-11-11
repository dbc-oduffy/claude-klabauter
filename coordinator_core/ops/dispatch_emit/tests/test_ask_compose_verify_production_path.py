"""ask_compose: the falsifier, judge and verify briefs require driving the production entry point."""

from __future__ import annotations

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


def test_shared_clause_names_every_requirement():
    _assert_clauses(ask_compose._PRODUCTION_PATH_CLAUSE)


def test_judge_brief_holds_the_clause():
    _assert_clauses(ask_compose._JUDGE_PATH_CLAUSE)


def test_falsifier_brief_holds_the_clause():
    _assert_clauses(ask_compose._FALSIFIER_ACT)


def test_verify_brief_holds_the_clause():
    _assert_clauses(ask_compose._VERIFY_INTRO)


def test_fixture_only_is_reported_not_met():
    assert "report it as not proving the criterion, never as met" in ask_compose._PRODUCTION_PATH_CLAUSE


def test_composed_script_carries_the_clause():
    _assert_clauses(_compose())


_OVERLAY_CMD = "-m coordinator_core.ops.dispatch_emit.clean_overlay_verify --run-id run-1"


def test_default_workflow_runs_the_overlay_producer_as_its_own_leg():
    text = _compose()
    assert _OVERLAY_CMD in text
    assert f"label: '{ask_compose.OVERLAY_VERIFY_LABEL}'" in text or f'label: "{ask_compose.OVERLAY_VERIFY_LABEL}"' in text


def test_overlay_producer_is_not_in_the_verify_agent_prompt():
    call = ask_compose._verify_call(
        head="h", anchor="a", test_js=ask_compose._test_instruction_js("HEAD", "REPO"), docs_parts=(),
        verdict_parts=("v",), judged=False, schema={}, run_id="run-1",
    )
    assert "clean_overlay_verify" not in call


def test_overlay_exit_one_folds_into_a_red_test_and_other_exits_do_not():
    fold = ask_compose._OVERLAY_FOLD_JS
    assert "_ov.exit_code === 1" in fold and "status: 'fail'" in fold
    assert "else if (_ov && _ov.exit_code !== 0)" in fold and "log(" in fold
    text = _compose()
    assert text.index("overlay-verify") < text.index("_testResult = _verify.test")
