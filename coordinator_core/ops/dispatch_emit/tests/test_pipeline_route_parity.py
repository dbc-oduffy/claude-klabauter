"""Graph parity: the pipeline route's emission for the structured fixture matches the frozen oracle's agent graph.

Prompt prose is not compared; only agent call sites (type, model, schema, fan-out) and subject keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.pipeline_graph import agent_graph, subjects

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_structured"
_ORACLE = _FIXTURE / "oracle" / "structured-research-fixture.workflow.mjs"


def _subject_keys() -> list[str]:
    lines = (_FIXTURE / "oracle" / "subjects.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _subject_objects() -> list[dict]:
    config = json.loads((_FIXTURE / "oracle" / "config.json").read_text(encoding="utf-8"))
    return [
        {
            "subject": key,
            "verifiers": [
                {"role": f"verifier-{topic['id']}", "topic": topic["id"], "name": topic["name"]}
                for topic in config["topics"]
            ],
        }
        for key in _subject_keys()
    ]


def _emit(monkeypatch, tmp_path: Path, content_root: Path) -> str:
    monkeypatch.setattr(op_module, "read_content_root_pointer", lambda: "doe")
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: content_root)
    out = tmp_path / "emitted.workflow.mjs"
    (tmp_path / "brief.md").write_text("parity check", encoding="utf-8")
    _dispatch_emit({
        "pipeline": "structured",
        "brief": "brief.md",
        "target_root": str(tmp_path),
        "session_id": "sess-parity",
        "subjects": _subject_objects(),
        "output_path": str(out),
    })
    return out.read_text(encoding="utf-8")


def test_emitted_graph_matches_oracle(monkeypatch, tmp_path):
    emitted = _emit(monkeypatch, tmp_path, _FIXTURE)
    oracle = _ORACLE.read_text(encoding="utf-8")

    graph = agent_graph(emitted)
    assert graph, "emitted script carries no agent call sites"
    assert graph == agent_graph(oracle)
    assert subjects(emitted) == subjects(oracle) == _subject_keys()


def test_mutated_fixture_breaks_parity(monkeypatch, tmp_path):
    import shutil

    content = tmp_path / "content"
    shutil.copytree(_FIXTURE / "pipelines", content / "pipelines")
    manifest = content / "pipelines" / "deep-research" / "structured.manifest.yaml"
    text = manifest.read_text(encoding="utf-8")
    assert "model: opus" in text
    manifest.write_text(text.replace("model: opus", "model: sonnet"), encoding="utf-8")

    work = tmp_path / "work"
    work.mkdir()
    emitted = _emit(monkeypatch, work, content)

    assert agent_graph(emitted) != agent_graph(_ORACLE.read_text(encoding="utf-8"))
