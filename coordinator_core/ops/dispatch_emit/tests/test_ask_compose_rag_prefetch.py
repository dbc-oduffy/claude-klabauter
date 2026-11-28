"""Engine-ask composer: a served repo opens with one rag-prefetch stage and every later brief carries the block; an unserved repo emits neither."""

from __future__ import annotations

import re

import pytest

from coordinator_core.bash_guards import _rag_serving
from coordinator_core.ops.dispatch_emit import ask_compose, pipeline_compose
from coordinator_core.ops.dispatch_emit import rag_prefetch as rp
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import AskPlanBlitzRefused
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_STUB_KW = dict(
    wrap_stage=lambda _t: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
    plan_blitz_text="stub",
    roadmap_blitz_text="export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\nreturn {};",
    **REVIEW_KW,
)
SPLICE = "(_ragBlock ? '\\n\\n' + _ragBlock : '')"
BLITZ_PREFIX = "(_ragBlock ? _ragBlock + '\\n\\n' : '')"
_HEADED_CALL = re.compile(r"_agentHead\(\) \+")
_HEAD_OPERAND = r"(?:_PFC\d+|'(?:[^'\\\n]|\\.)*')"  # hoisted constant, or the inline literal when the suite turns hoisting off
_HEADED_WITH_SPLICE = re.compile(r"_agentHead\(\) \+ " + _HEAD_OPERAND + r" \+ " + re.escape(SPLICE))


def _compose(monkeypatch: pytest.MonkeyPatch, *, served: bool) -> str:
    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: served)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    kw = dict(repo_root="REPO", prompt="do a big thing", sizing_rel=None, run_id="run-1", session_id=None, script_path="x.mjs")
    try:
        return ask_compose.compose_ask_script(**kw)
    except (AskPlanBlitzRefused, ask_compose.AskComposeRefused):
        return ask_compose.compose_ask_script(**kw, **_STUB_KW)


def test_served_repo_opens_with_one_prefetch_stage_ahead_of_sizing(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _compose(monkeypatch, served=True)
    assert script.count("label: 'rag-prefetch'") == 1
    assert script.index("phase('rag-prefetch')") < script.index("label: 'rag-prefetch'") < script.index("phase('size')")
    assert f"agentType: '{pipeline_compose.RAG_PREFETCH_AGENT_TYPE}'" in script
    assert "'rag-prefetch', 'size'" in script


def test_every_later_stage_brief_carries_the_block_once(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _compose(monkeypatch, served=True)
    headed = len(_HEADED_CALL.findall(script))
    assert headed > 5
    assert len(_HEADED_WITH_SPLICE.findall(script)) == headed
    assert script.count(rp.PREFETCH_BLOCK_MARK) == 1
    assert script.count("let _ragBlock = '';") == 1
    assert f"const _RAG_CAP = {rp.PREFETCH_BLOCK_CHAR_CAP};" in script


def test_plan_blitz_and_roadmap_blitz_agents_carry_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _compose(monkeypatch, served=True)
    assert script.count(BLITZ_PREFIX) == 2


def test_prefetch_failure_logs_and_never_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _compose(monkeypatch, served=True)
    start = script.index("phase('rag-prefetch')")
    guarded = script[start : script.index("phase('size')")]
    assert "try {" in guarded and "} catch (err) {" in guarded
    assert "run continues without the block" in guarded
    assert "throw" not in guarded
    assert "_halted" not in guarded


def test_unserved_repo_emits_neither_stage_nor_block(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _compose(monkeypatch, served=False)
    assert "rag-prefetch" not in script
    assert "_ragBlock" not in script
    assert rp.PREFETCH_BLOCK_MARK not in script


@pytest.mark.parametrize("served", [True, False])
def test_return_carries_rag_use_from_the_run_op(monkeypatch: pytest.MonkeyPatch, served: bool) -> None:
    script = _compose(monkeypatch, served=served)
    assert "engineOp('dispatch.run_rag_use'" in script
    assert "rag_degraded: _ragDegraded, rag_use: _runRagUse," in script
    assert script.count("let _runRagUse = null;") == 1
    assert script.index("engineOp('dispatch.run_rag_use'") < script.index("rag_use: _runRagUse")
