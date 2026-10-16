"""The planner prefix names the inbox memo an ask or its sizing names as the plan's source_memo."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_MEMO = "state/cross-repo/inbox/2026-10-16-x.md"


def _script(prompt, ask_source="relayed"):
    return compose_ask_script(
        repo_root="REPO",
        prompt=prompt,
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        ask_source=ask_source,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )


def _prefix_line(script):
    return next(ln for ln in script.splitlines() if ln.startswith("  const _LEDGER_PREFIX ="))


def test_relayed_ask_naming_one_inbox_memo_carries_the_clause():
    line = _prefix_line(_script(f"Fix X. Close the memo on landing: {_MEMO}."))
    assert f"Source memo: set plan frontmatter `source_memo: {_MEMO}`" in line
    assert "author no row that edits, closes or sends it" in line


def test_a_pm_ask_carries_the_clause_too():
    assert "Source memo:" in _prefix_line(_script(f"see {_MEMO}", ask_source="pm"))


def test_ask_naming_no_memo_leaves_the_prefix_without_a_clause():
    assert "Source memo" not in _script("Fix X, no memo.")


def test_placeholder_is_not_a_memo():
    assert "Source memo" not in _script("Engine defect: state/cross-repo/inbox/<memo>.md is ignored")


def test_other_repo_inbox_path_is_not_a_memo():
    assert "Source memo" not in _script("see coordinator-content-repo/state/cross-repo/inbox/a.md")


def test_two_memos_list_both_and_name_the_rest_in_an_open_question():
    line = _prefix_line(_script("state/cross-repo/inbox/a.md and state/cross-repo/inbox/b.md"))
    assert "Source memos named: `state/cross-repo/inbox/a.md`, `state/cross-repo/inbox/b.md`" in line
    assert "open question" in line


def test_sizing_register_row_text_supplies_the_memo(monkeypatch):
    sizing = {"requirement_register": {"rows": [{"source_text": f"close {_MEMO} on landing"}]}}
    monkeypatch.setattr(ask_compose, "load_sizing", lambda *_: sizing)
    assert f"`source_memo: {_MEMO}`" in ask_compose._source_memo_clause("REPO", None, "state/sizings/x.yaml")


def test_unreadable_sizing_yields_no_clause(monkeypatch):
    def boom(*_):
        raise OSError("gone")

    monkeypatch.setattr(ask_compose, "load_sizing", boom)
    assert ask_compose._source_memo_clause("REPO", None, "state/sizings/x.yaml") == ""
