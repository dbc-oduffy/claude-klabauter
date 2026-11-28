"""Research-run composer: a served repo opens with one rag-prefetch stage and every later brief carries the block; an unserved repo emits neither."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.bash_guards import _rag_serving
from coordinator_core.ops.dispatch_emit import pipeline_compose as pc
from coordinator_core.ops.dispatch_emit import rag_prefetch as rp
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script, compose_pipeline_script
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineInputs
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate
from coordinator_core.ops.dispatch_emit.tests.pipeline_graph import agent_graph

SPLICE = "+ (_ragBlock ? '\\n\\n' + _ragBlock : '')"


def _stage(sid, **over):
    base = {
        "id": sid, "agent_type": "coordinator:research-scout", "model": "haiku",
        "template": "t.md", "output": "{{scratch_dir}}/" + sid + ".md", "fan_out": "none",
    }
    base.update(over)
    return base


def _segment(tmp_path: Path, pipeline: str, stages: list[dict]):
    root = tmp_path / "content" / "pipelines" / "dr"
    root.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": 1, "pipeline": pipeline, "inputs": {"brief": "required"}, "stages": stages}
    (root / f"{pipeline}.manifest.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    (root / "t.md").write_text("work from {{brief}} in {{scratch_dir}}", encoding="utf-8")
    manifest = load_manifest(tmp_path / "content", pipeline)
    inputs = PipelineInputs(brief="state/ask.md", subjects=(), scratch_dir="scratch/run", flags={})
    return manifest, inputs, validate(manifest, inputs)


def _served(monkeypatch, served: bool) -> None:
    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: served)


def _three(tmp_path):
    return _segment(tmp_path, "p", [_stage("plan"), _stage("a", depends_on=["plan"]), _stage("b", depends_on=["plan"])])


def test_served_repo_opens_with_one_prefetch_stage_before_the_first_stage(tmp_path, monkeypatch):
    _served(monkeypatch, True)
    script = compose_pipeline_script(*_three(tmp_path), run_id="r1", agent_type_host=None, rag_root=str(tmp_path))
    assert script.count("label: 'rag-prefetch'") == 1
    assert script.index("label: 'rag-prefetch'") < script.index("work from state/ask.md")
    assert "'rag-prefetch'" in script.split("phases:")[1].split("]")[0]
    assert len(agent_graph(script)) == 4
    assert script.count("(RELAY + REVISIONS + '\\n\\n' + ") == len(agent_graph(script))


def test_every_later_stage_brief_carries_the_block_once(tmp_path, monkeypatch):
    _served(monkeypatch, True)
    script = compose_pipeline_script(*_three(tmp_path), run_id="r1", agent_type_host=None, rag_root=str(tmp_path))
    assert script.count(SPLICE) == 3
    assert script.count(rp.PREFETCH_BLOCK_MARK) == 1
    assert script.count("let _ragBlock = '';") == 1
    assert f"const _RAG_CAP = {rp.PREFETCH_BLOCK_CHAR_CAP};" in script


def test_prefetch_failure_logs_and_never_blocks(tmp_path, monkeypatch):
    _served(monkeypatch, True)
    script = compose_pipeline_script(*_three(tmp_path), run_id="r1", agent_type_host=None, rag_root=str(tmp_path))
    start = script.index("phase('rag-prefetch')")
    guarded = script[start:script.index("work from state/ask.md")]
    assert "try {" in guarded and "} catch (err) {" in guarded
    assert "run continues without the block" in guarded
    assert "throw" not in guarded


def test_unserved_repo_emits_neither_stage_nor_block(tmp_path, monkeypatch):
    _served(monkeypatch, False)
    script = compose_pipeline_script(*_three(tmp_path), run_id="r1", agent_type_host=None, rag_root=str(tmp_path))
    assert "rag-prefetch" not in script
    assert "_ragBlock" not in script
    assert rp.PREFETCH_BLOCK_MARK not in script
    assert len(agent_graph(script)) == 3


def test_no_rag_root_emits_neither(tmp_path, monkeypatch):
    _served(monkeypatch, True)
    script = compose_pipeline_script(*_three(tmp_path), run_id="r1", agent_type_host=None)
    assert "rag-prefetch" not in script and "_ragBlock" not in script


def test_chain_prefetches_once_before_every_segment(tmp_path, monkeypatch):
    _served(monkeypatch, True)
    first = _segment(tmp_path, "one", [_stage("x")])
    second = _segment(tmp_path, "two", [_stage("y")])
    script = compose_chain_script([first, second], run_id="r1", agent_type_host=None, rag_root=str(tmp_path))
    assert script.count("label: 'rag-prefetch'") == 1
    assert script.index("label: 'rag-prefetch'") < script.index("segment1:")
    assert script.count(SPLICE) == 2


def test_bound_mirrors_python_citation_block_constants():
    assert pc._rag_prefetch.PREFETCH_BLOCK_CHAR_CAP == rp.PREFETCH_BLOCK_CHAR_CAP
    assert "split(_RAG_MARK).join('')" in pc._RAG_BOUND_JS


@pytest.mark.parametrize("host", [None])
def test_prefetch_agent_is_a_pinned_roster_type(tmp_path, monkeypatch, host):
    _served(monkeypatch, True)
    script = compose_pipeline_script(*_three(tmp_path), run_id="r1", agent_type_host=host, rag_root=str(tmp_path))
    assert agent_graph(script)[0]["agentType"] == pc.RAG_PREFETCH_AGENT_TYPE
