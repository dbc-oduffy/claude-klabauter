"""ask_compose: the falsifier, judge and verify briefs require driving the production entry point."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_ENTRY_POINT = "drive that production entry point end-to-end"
_FOREIGN_CWD = "from a foreign cwd (not the repo root) where cwd matters"
_FIXTURE_ONLY = "exercises only route-authored test fixtures"
_NOT_PROVING = "does not prove the criterion"
_CLAUSES = (_ENTRY_POINT, _FOREIGN_CWD, _FIXTURE_ONLY, _NOT_PROVING)


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
