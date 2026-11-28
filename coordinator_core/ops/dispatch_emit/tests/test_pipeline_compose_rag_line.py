"""Composer: every engine-spawned pipeline worker prompt states example-retrieval-repo-first on a served repo, none on an unserved one."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bash_guards import _rag_serving
from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_chain_script, compose_pipeline_script
from coordinator_core.ops.dispatch_emit.tests.pipeline_graph import agent_graph
from coordinator_core.ops.dispatch_emit.tests.test_pipeline_compose_brief_and_halt import _segment, _stage

_MARK = "Code lookup: example-retrieval-repo first."
_PROMPT_HEAD = "(RELAY + REVISIONS + '\\n\\n' + "


def _script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: bool, rag_root: str | None) -> str:
    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: served)
    manifest, inputs, schedule = _segment(tmp_path, "p", [_stage("a"), _stage("b", depends_on=["a"])])
    return compose_pipeline_script(manifest, inputs, schedule, run_id="r1", agent_type_host=None, rag_root=rag_root)


def test_served_repo_worker_prompts_carry_the_line_through_revisions(tmp_path, monkeypatch):
    script = _script(tmp_path, monkeypatch, True, tmp_path.as_posix())
    assert script.count(_MARK) == 1
    assert script.count("const REVISIONS = ") == 1 and _MARK in script.split("const REVISIONS = ", 1)[1].split("\n", 1)[0]
    assert script.count(_PROMPT_HEAD) == len(agent_graph(script)) == 2


def test_unserved_repo_carries_no_line(tmp_path, monkeypatch):
    assert _MARK not in _script(tmp_path, monkeypatch, False, tmp_path.as_posix())


def test_no_rag_root_carries_no_line_even_when_served(tmp_path, monkeypatch):
    assert _MARK not in _script(tmp_path, monkeypatch, True, None)


def test_chain_close_agent_prompt_does_not_carry_the_line(tmp_path, monkeypatch):
    monkeypatch.setattr(_rag_serving, "serves_repo", lambda cwd=None: True)
    close = {"op": "research.close", "params": {"run_id": "r1"}}
    script = compose_chain_script(
        [_segment(tmp_path, "main", [_stage("work")])], run_id="r1", agent_type_host=None,
        next_action=close, rag_root=tmp_path.as_posix(),
    )
    relay = script.split("const closeInRun", 1)[1]
    assert _MARK not in relay
