"""A UE repo's composed run docs-checks the plan and the diff before review, each leg gated on example-retrieval-repo use."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import agent_route, ask_compose, ue_route_source
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_CITATION_UNBACKED, HALT_RAG_CORPUS_DOWN
from coordinator_core.ops.dispatch_emit.rag_use import RESTORE_MESSAGE
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


def test_plan_docs_check_and_gate_precede_plan_review(monkeypatch, tmp_path):
    script = _m_plus(monkeypatch, _ue_declared_repo(tmp_path))
    assert _label_at(script, "plan-docs-check") < _label_at(script, "plan-docs-check-gate") < _label_at(script, "plan-review")
    assert script.count(GATE_OP) >= 2


def test_diff_docs_check_and_gate_precede_the_code_review_wave(tmp_path):
    script = _compose(repo_root=_ue_declared_repo(tmp_path), **_named_fragment())
    assert _label_at(script, "diff-docs-check") < _label_at(script, "diff-docs-check-gate")
    assert _label_at(script, "diff-docs-check-gate") < script.index("phase('Review wave')")
    assert "label: 'review:coordinator:docs-checker'" not in script


def test_a_non_ue_compose_carries_no_docs_check_leg(monkeypatch, tmp_path):
    (tmp_path / "coordinator.local.md").write_text("---\nproject_type: web\n---\n", encoding="utf-8")
    root = str(tmp_path)
    for script in (_m_plus(monkeypatch, root), _compose(repo_root=root, **_named_fragment())):
        assert "docs-check" not in script and GATE_OP not in script
        assert "rag_corpus_down" not in script


def test_the_checker_schema_admits_rag_down_and_degraded():
    assert ask_compose._DOCS_CHECK_SCHEMA["properties"]["verdict"]["enum"] == ["ok", "corrected", "rag_down", "degraded"]
    assert "degraded_bands" in ask_compose._DOCS_CHECK_SCHEMA["properties"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to evaluate the composed halt lines")
@pytest.mark.parametrize(
    ("leg", "check_var", "gate_var"),
    [("plan", "_planDocsCheck", "_planDocsGate"), ("diff", "_diffDocsCheck", "_diffDocsGate")],
)
def test_each_leg_halts_on_the_gate_and_checker_verdicts(monkeypatch, tmp_path, leg, check_var, gate_var):
    root = _ue_declared_repo(tmp_path)
    script = _m_plus(monkeypatch, root) if leg == "plan" else _compose(repo_root=root, **_named_fragment())
    fragments = re.findall(r"^(?:const|let|var) _PF\w+ = .*;$", script, re.M)
    halt = [ln.strip() for ln in script.splitlines() if ln.strip().startswith((f"if (({check_var}", f"else if ({gate_var}"))]
    assert len(halt) == 4
    harness = (
        "\n".join(fragments)
        + "\nlet _halted = null; let _ragDegraded = []; const _runId = 'r', _sizingRel = 's'; let _planRel = 'p'; const _gate = { spine_plan: 'p' };\n"
        + f"function run(chk, gate) {{ _halted = null; _ragDegraded = []; const {check_var} = chk, {gate_var} = gate;\n"
        + "\n".join(halt)
        + "\n return _halted || { continued: _ragDegraded }; }\n"
        "console.log(JSON.stringify([\n"
        "  run({verdict:'ok'}, {verdict:'degraded', rag_calls:2, rag_failed:1, rag_timeouts:1, rag_refused:0, reason:'t'}),\n"
        "  run({verdict:'ok'}, {verdict:'ok'}),\n"
        "  run({verdict:'ok'}, {verdict:'rag_corpus_down'}),\n"
        "  run({verdict:'rag_down'}, {verdict:'ok'}),\n"
        "  run({verdict:'ok'}, {verdict:'citation_unbacked', unbacked:['[rag: project_symbol Foo]']}),\n"
        "  run({verdict:'ok'}, {}),\n"
        "  run({verdict:'rag_down'}, {verdict:'degraded', rag_calls:2, rag_failed:2, rag_timeouts:2, rag_refused:0, reason:'t'}),\n"
        "  run({verdict:'degraded', degraded_bands:['engine-source'], reason:'d'}, {verdict:'not_applicable'}),\n"
        "  run({verdict:'rag_down'}, {verdict:'not_applicable'}),\n"
        "  run({verdict:'degraded', degraded_bands:['engine-source']}, {verdict:'ok'}),\n"
        "]));"
    )
    script_path = tmp_path / "halt_harness.js"
    script_path.write_text(harness, encoding="utf-8")
    out = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=True).stdout
    (degraded, ok, down, rag_down, unbacked, no_verdict, checker_down_gate_degraded,
     checker_degraded_na, checker_down_na, checker_degraded_ok) = json.loads(out)
    checker_entry = {"leg": f"{leg}-docs-check", "source": "checker", "degraded_bands": ["engine-source"]}
    assert checker_degraded_na == {"continued": [{**checker_entry, "reason": "d"}]}
    assert checker_down_na == {"continued": []}
    assert checker_degraded_ok == {"continued": [{**checker_entry, "reason": None}]}
    assert degraded == {"continued": [{
        "leg": f"{leg}-docs-check", "rag_calls": 2, "rag_failed": 1, "rag_timeouts": 1, "rag_refused": 0, "reason": "t",
    }]}
    assert ok == {"continued": []}
    assert checker_down_gate_degraded == {"continued": [{
        "leg": f"{leg}-docs-check", "rag_calls": 2, "rag_failed": 2, "rag_timeouts": 2, "rag_refused": 0, "reason": "t",
    }]}
    for halted in (down, rag_down, no_verdict):
        assert halted["halted"] == HALT_RAG_CORPUS_DOWN and halted["reason"] == RESTORE_MESSAGE
        assert halted["next_action"] == RESTORE_MESSAGE
    assert unbacked["halted"] == HALT_CITATION_UNBACKED
    assert "[rag: project_symbol Foo]" in unbacked["reason"] and unbacked["unbacked"] == ["[rag: project_symbol Foo]"]
