"""A rag-served repo's composed script carries the example-retrieval-repo-first line once; an unserved repo's does not."""

from __future__ import annotations

from coordinator_core.bash_guards import _rag_serving
from coordinator_core.bash_guards._rewrite_support import _EXAMPLE_RETRIEVAL_REPO_HINT
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.emit import emit_script

from .conftest import REVIEW_KW

_MARK = "Code lookup: example-retrieval-repo first."


def _plan_file(tmp_path):
    out = ""
    for row_id in ("C1", "C2"):
        out += (
            f"- id: {row_id}\n  title: t{row_id}\n  change_kind: doc-edit\n"
            f"  surface: docs/x/{row_id}.md\n  writes:\n    - docs/x/{row_id}.md\n"
            f"  queue_scope: project\n  disposition: open\n  body: |\n    Do {row_id}.\n"
        )
    path = tmp_path / "p.md"
    path.write_text(
        '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n'
        f"## Tasks\n\n```yaml plan-tasks\n{out}```\n",
        encoding="utf-8",
    )
    return path


def _script(tmp_path, monkeypatch, served: bool) -> str:
    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: served)
    return emit_script(_plan_file(tmp_path), repo_root=tmp_path, **REVIEW_KW)


def test_served_repo_script_states_the_line_exactly_once(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    (plugin / "snippets").mkdir(parents=True)
    (plugin / "snippets" / "example-retrieval-repo-preamble.md").write_text(
        "**Code lookup: example-retrieval-repo first.**\n\n"
        '1. First action: `ToolSearch("select:mcp__project-rag__project_staleness_check")`\n'
        "2. Then something else.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    script = _script(tmp_path, monkeypatch, True)
    assert script.count(_MARK) == 1
    assert script.count("mcp__project-rag__project_staleness_check") == 1


def test_unserved_repo_script_has_no_line(tmp_path, monkeypatch):
    script = _script(tmp_path, monkeypatch, False)
    assert _MARK not in script
    assert "mcp__project-rag__project_staleness_check" not in script


def test_unreadable_snippet_falls_back_to_the_hint(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.subagent_sandbox.plugin_root.resolve_plugin_root", lambda *a, **k: None
    )
    assert emit._example_retrieval_repo_first_line() == _EXAMPLE_RETRIEVAL_REPO_HINT


def _ask_script(monkeypatch, served: bool) -> str:
    from coordinator_core.ops.dispatch_emit import ask_compose, prompt_fragments
    from coordinator_core.ops.dispatch_emit.ask_plan_blitz import AskPlanBlitzRefused
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_prompt_fragments import _STUB_KW

    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: served)
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", False)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    kw = dict(repo_root="REPO", prompt="do a big thing", sizing_rel=None, run_id="run-1", session_id=None, script_path="x.mjs")
    try:
        return ask_compose.compose_ask_script(**kw)
    except AskPlanBlitzRefused:
        return ask_compose.compose_ask_script(**kw, **_STUB_KW)


def test_served_ask_briefs_carry_the_line_and_the_fallback_rule(monkeypatch):
    script = _ask_script(monkeypatch, True)
    assert script.count(_MARK) >= 3
    assert script.count("retry it once, then fall back to scoped grep and report the fallback") == script.count(_MARK)


def test_unserved_ask_briefs_carry_neither(monkeypatch):
    script = _ask_script(monkeypatch, False)
    assert _MARK not in script
    assert "fall back to scoped grep" not in script


def test_preamble_already_carrying_the_line_is_not_doubled(monkeypatch):
    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: True)
    once = emit._with_example_retrieval_repo_line("p", "REPO")
    assert emit._with_example_retrieval_repo_line(once, "REPO") == once


def test_served_without_repo_root_leaves_preamble_untouched(monkeypatch):
    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: True)
    assert emit._with_example_retrieval_repo_line("p", None) == "p"
