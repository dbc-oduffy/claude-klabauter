"""A UE repo's composed run docs-checks the plan and the diff before review, each leg gated on example-retrieval-repo use."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit import agent_route, ask_compose, leg_register, ue_route_source
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose, _presized
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_agent_routing import (
    _named_fragment,
    _ue_declared_repo,
)

GATE_OP = "dispatch.docs_check_rag_gate"


@pytest.fixture(autouse=True)
def _ue(monkeypatch):
    monkeypatch.setattr(agent_route, "_model_of", lambda agent_type, defs, override=None: "opus")
    ue_route_source.clear_caches()
    yield
    ue_route_source.clear_caches()


def _m_plus(monkeypatch, root):
    return _presized(monkeypatch, arm="m_plus", repo_root=root, **_named_fragment())


def _label_at(script, label):
    return script.index(f"label: '{label}'")


def _checker_prompt_span(script, label):
    """The composed agent call of the `label` docs-checker leg, from its prompt to its label."""
    end = _label_at(script, label)
    return script[script.rindex("agent(", 0, end):end]


def test_plan_docs_check_runs_the_gate_itself_ahead_of_plan_review(monkeypatch, tmp_path):
    script = _m_plus(monkeypatch, _ue_declared_repo(tmp_path))
    assert "label: 'plan-docs-check-gate'" not in script
    assert _label_at(script, "plan-docs-check") < _label_at(script, "plan-review")
    assert GATE_OP in _checker_prompt_span(script, "plan-docs-check")


def test_diff_docs_check_runs_the_gate_itself_ahead_of_the_code_review_wave(tmp_path):
    script = _compose(repo_root=_ue_declared_repo(tmp_path), **_named_fragment())
    assert "label: 'diff-docs-check-gate'" not in script
    assert _label_at(script, "diff-docs-check") < script.index("phase('Review wave')")
    assert GATE_OP in _checker_prompt_span(script, "diff-docs-check")
    assert "label: 'review:coordinator:docs-checker'" not in script


def test_a_non_ue_compose_carries_no_docs_check_leg(monkeypatch, tmp_path):
    (tmp_path / "coordinator.local.md").write_text("---\nproject_type: web\n---\n", encoding="utf-8")
    root = str(tmp_path)
    for script in (_m_plus(monkeypatch, root), _compose(repo_root=root, **_named_fragment())):
        assert "docs-check" not in script and GATE_OP not in script
        assert "rag_corpus_down" not in script


def test_a_ue_repo_plan_leg_is_skipped_only_when_declared_writes_touch_no_ue_path(monkeypatch, tmp_path):
    root = _ue_declared_repo(tmp_path)
    py_only = _presized(monkeypatch, arm="m_plus", repo_root=root, writes=["tools/x/labeller.py", "docs/a.md"],
                        **_named_fragment())
    assert "label: 'plan-docs-check'" not in py_only
    assert "gap: 'no_ue_paths'" in py_only and "leg: 'plan-docs-check'" in py_only
    ue = _presized(monkeypatch, arm="m_plus", repo_root=root, writes=["tools/x.py", "Source/G/A.cpp"],
                   **_named_fragment())
    assert "label: 'plan-docs-check'" in ue and "leg: 'plan-docs-check', gap: 'no_ue_paths'" not in ue
    assert "label: 'plan-docs-check'" in _m_plus(monkeypatch, root)


def test_the_diff_leg_is_guarded_by_the_ue_path_test_and_keeps_its_agent_call(tmp_path):
    script = _compose(repo_root=_ue_declared_repo(tmp_path), **_named_fragment())
    assert "label: 'diff-docs-check'" in script
    assert "const _runTouchesUe" in script
    assert script.index("if (!_runTouchesUe)") < _label_at(script, "diff-docs-check")


def test_the_checker_schema_admits_rag_down_and_degraded():
    assert ask_compose._DOCS_CHECK_SCHEMA["properties"]["verdict"]["enum"] == ["ok", "corrected", "rag_down", "degraded"]
    assert "degraded_bands" in ask_compose._DOCS_CHECK_SCHEMA["properties"]
    assert ask_compose._DOCS_CHECK_SCHEMA["properties"]["gate"] is ask_compose._DOCS_GATE_SCHEMA
    assert "gate" not in ask_compose._DOCS_CHECK_SCHEMA["required"]


@pytest.mark.parametrize("leg", ["plan", "diff"])
def test_each_leg_composes_a_distinctly_labelled_retry_whose_gate_names_that_label(monkeypatch, tmp_path, leg):
    root = _ue_declared_repo(tmp_path)
    script = _m_plus(monkeypatch, root) if leg == "plan" else _compose(repo_root=root, **_named_fragment())
    labels = [leg_.label for leg_ in leg_register.emitted_legs(script)]
    assert labels.count(f"{leg}-docs-check") == 1 and labels.count(f"{leg}-docs-check-retry") == 1
    assert script.count(f"label: '{leg}-docs-check-retry'") == 2


def _staged_diff_docs_bodies(tmp_path):
    sink: dict[str, str] = {}
    script = _compose(repo_root=_ue_declared_repo(tmp_path), staged_out=sink, **_named_fragment())
    return script, {n: t for n, t in sink.items() if "Docs-check this run's diff" in t}


def test_the_staged_diff_docs_check_prompt_ends_in_a_complete_sentence_naming_the_review_paths(tmp_path):
    script, bodies = _staged_diff_docs_bodies(tmp_path)
    assert bodies
    for text in bodies.values():
        assert text.rstrip().endswith(".")
        assert "to the review paths listed after this brief" in text
    span = _checker_prompt_span(script, "diff-docs-check")
    assert "_reviewPaths.join(', ')" in span
    assert span.index("_reviewPaths.join") < span.index(GATE_OP)
