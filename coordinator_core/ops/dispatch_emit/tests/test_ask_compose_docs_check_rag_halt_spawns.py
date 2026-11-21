"""A UE repo's composed run docs-checks the plan and the diff before review, each leg gated on example-retrieval-repo use."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import agent_route, ue_route_source
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_CITATION_UNBACKED, HALT_RAG_CORPUS_DOWN
from coordinator_core.ops.dispatch_emit.rag_use import RESTORE_MESSAGE
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose, _presized
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_agent_routing import (
    _named_fragment,
    _ue_declared_repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

GATE_OP = "dispatch.docs_check_rag_gate"


@pytest.fixture(autouse=True)
def _ue(monkeypatch):
    monkeypatch.setattr(agent_route, "_model_of", lambda agent_type, defs, override=None: "opus")
    ue_route_source.clear_caches()
    yield
    ue_route_source.clear_caches()


def _m_plus(monkeypatch, root):
    return _presized(monkeypatch, arm="m_plus", repo_root=root, **_named_fragment())


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to evaluate the composed skip guard")
def test_the_composed_diff_guard_skips_python_docs_and_runs_ue_paths(tmp_path):
    script = _compose(repo_root=_ue_declared_repo(tmp_path), **_named_fragment())
    start = script.index("const _uePathRe")
    guard = script[start:script.index(";\n", script.index("const _runTouchesUe")) + 1]
    harness = (
        "function run(paths, writes) { const _reviewPaths = paths, _writes = writes;\n"
        + guard + "\n return _runTouchesUe; }\n"
        "console.log(JSON.stringify([\n"
        "  run(['tools/likeness-prototype/likeness/hairstyle_class/x.py','docs/c.md','schema/b.json'], []),\n"
        "  run(['a.py'], ['Plugins/P/Source/P/Y.cpp']),\n"
        "  run(['control\\\\plugin\\\\Z.txt'], []),\n"
        "  run(['a.uasset'], null),\n"
        "]))"
    )
    path = tmp_path / "guard.js"
    path.write_text(harness, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [False, True, True, True]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to evaluate the composed halt lines")
@pytest.mark.parametrize(
    ("leg", "check_var", "gate_var"),
    [("plan", "_planDocsCheck", "_planDocsGate"), ("diff", "_diffDocsCheck", "_diffDocsGate")],
)
def test_each_leg_retries_a_corpus_down_once_then_degrades_and_halts_only_on_citations(
    monkeypatch, tmp_path, leg, check_var, gate_var
):
    root = _ue_declared_repo(tmp_path)
    script = _m_plus(monkeypatch, root) if leg == "plan" else _compose(repo_root=root, **_named_fragment())
    fragments = re.findall(r"^(?:const|let|var) _PF\w+ = .*;$", script, re.M)
    c, g = check_var, gate_var
    wanted = (
        f"const {c}Gap", f"const {c}Down", f"let {c}Attempts", f"if ({c}Down()) {{", f"if ({c}Gap())",
        f"else if ({c}Down())", f"else if ({g}.verdict",
    )
    lines = [ln.strip() for ln in script.splitlines() if ln.strip().startswith(wanted)]
    assert len(lines) == 9
    harness = (
        "\n".join(fragments)
        + "\nlet _halted = null; let _ragDegraded = []; const _runId = 'r', _sizingRel = 's'; let _planRel = 'p'; const _gate = { spine_plan: 'p' };\n"
        + f"async function run(chk, gate, chk2, gate2) {{ _halted = null; _ragDegraded = []; let retries = 0;\n"
        f"let {c} = chk, {g} = gate; const {c}Retry = () => {{ retries++; return {{ ...chk2, gate: gate2 }}; }};\n"
        + "\n".join(lines)
        + "\n return { out: _halted || { continued: _ragDegraded }, retries }; }\n"
        "const OK = {verdict:'ok'}, DOWN = {verdict:'rag_corpus_down', reason:'refused', branch:'all_calls_refused'};\n"
        "const CITE = {verdict:'citation_unbacked', unbacked:['[rag: project_symbol Foo]']};\n"
        "(async () => console.log(JSON.stringify([\n"
        "  await run({verdict:'ok'}, {verdict:'degraded', rag_calls:2, rag_failed:1, rag_timeouts:1, rag_refused:0, reason:'t'}, OK, OK),\n"
        "  await run({verdict:'ok'}, OK, OK, OK),\n"
        "  await run({verdict:'ok'}, DOWN, OK, DOWN),\n"
        "  await run({verdict:'rag_down'}, OK, {verdict:'rag_down'}, OK),\n"
        "  await run({verdict:'ok'}, CITE, OK, OK),\n"
        "  await run({verdict:'ok'}, {}, OK, {}),\n"
        "  await run({verdict:'rag_down'}, {verdict:'degraded', rag_calls:2, rag_failed:2, rag_timeouts:2, rag_refused:0, reason:'t'}, OK, OK),\n"
        "  await run({verdict:'degraded', degraded_bands:['engine-source'], reason:'d'}, {verdict:'not_applicable'}, OK, OK),\n"
        "  await run({verdict:'rag_down'}, {verdict:'not_applicable'}, OK, OK),\n"
        "  await run({verdict:'degraded', degraded_bands:['engine-source']}, OK, OK, OK),\n"
        "  await run({verdict:'rag_down'}, {verdict:'rag_not_used', branch:'zero_rag_calls', reason:'no calls', rag_calls:0}, OK, OK),\n"
        "  await run({verdict:'ok'}, {verdict:'transcript_unreadable', branch:'transcript_missing', reason:'gone'}, OK, OK),\n"
        "  await run({verdict:'ok'}, DOWN, OK, OK),\n"
        "  await run({verdict:'ok'}, DOWN, OK, CITE),\n"
        "  await run({verdict:'degraded', degraded_bands:['engine-source'], reason:'d'}, null, {verdict:'degraded', degraded_bands:['engine-source'], reason:'d'}, undefined),\n"
        "  await run({verdict:'degraded', degraded_bands:['engine-source'], reason:'d'}, undefined, {verdict:'degraded', degraded_bands:['engine-source'], reason:'d'}, OK),\n"
        "])))();"
    )
    script_path = tmp_path / "halt_harness.js"
    script_path.write_text(harness, encoding="utf-8")
    out = subprocess.run(["node", str(script_path)], capture_output=True, text=True, check=True).stdout
    (degraded, ok, down, rag_down, unbacked, no_verdict, checker_down_gate_degraded, checker_degraded_na,
     checker_down_na, checker_degraded_ok, not_used, unreadable, recovered, cite_after_retry, degraded_gateless,
     degraded_gate_returns) = json.loads(out)
    name = f"{leg}-docs-check"
    checker_entry = {"leg": name, "source": "checker", "degraded_bands": ["engine-source"]}
    assert checker_degraded_na == {"out": {"continued": [{**checker_entry, "reason": "d"}]}, "retries": 0}
    assert checker_down_na == {"out": {"continued": []}, "retries": 0}
    assert checker_degraded_ok == {"out": {"continued": [{**checker_entry, "reason": None}]}, "retries": 0}
    assert degraded == {"out": {"continued": [{
        "leg": name, "rag_calls": 2, "rag_failed": 1, "rag_timeouts": 1, "rag_refused": 0, "reason": "t",
    }]}, "retries": 0}
    assert ok == {"out": {"continued": []}, "retries": 0}
    assert checker_down_gate_degraded == {"out": {"continued": [{
        "leg": name, "rag_calls": 2, "rag_failed": 2, "rag_timeouts": 2, "rag_refused": 0, "reason": "t",
    }]}, "retries": 0}
    outage = {"leg": name, "gap": HALT_RAG_CORPUS_DOWN, "attempts": 2, "next_action": RESTORE_MESSAGE}
    assert down == {"out": {"continued": [{**outage, "branch": "all_calls_refused", "reason": "refused"}]}, "retries": 1}
    assert rag_down == {"out": {"continued": [{**outage, "branch": None, "reason": None}]}, "retries": 1}
    assert no_verdict == {"out": {"continued": [{**outage, "branch": None, "reason": None}]}, "retries": 1}
    assert not_used == {"out": {"continued": [{
        "leg": name, "gap": "rag_not_used", "branch": "zero_rag_calls", "reason": "no calls", "rag_calls": 0,
    }]}, "retries": 0}
    assert unreadable == {"out": {"continued": [{
        "leg": name, "gap": "transcript_unreadable", "branch": "transcript_missing", "reason": "gone", "rag_calls": None,
    }]}, "retries": 0}
    assert recovered == {"out": {"continued": []}, "retries": 1}
    for structural in (unbacked, cite_after_retry):
        assert structural["out"]["halted"] == HALT_CITATION_UNBACKED
        assert "[rag: project_symbol Foo]" in structural["out"]["reason"]
        assert structural["out"]["unbacked"] == ["[rag: project_symbol Foo]"]
    assert unbacked["retries"] == 0 and cite_after_retry["retries"] == 1
    assert "halted" not in degraded_gateless["out"]
    assert degraded_gateless == {"out": {"continued": [{**outage, "branch": None, "reason": "d"}]}, "retries": 1}
    assert degraded_gate_returns == {"out": {"continued": [{**checker_entry, "reason": "d"}]}, "retries": 1}
