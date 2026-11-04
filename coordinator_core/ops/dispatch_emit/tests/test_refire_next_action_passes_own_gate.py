"""An engine-prescribed refire of a target-less repo research sizing passes dispatch.emit's own gate.

The sizing is written by sizing-assemble's default entry (`--research-source repo`, no target); each
producer of a halt `next_action`/`fire` call is built, its `sizing` arg is fed to `dispatch.emit` as the
submit hook does (`from_sizing`), and the emit must not refuse and must resolve the firing repo.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import ask_contract, halt_refire
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _Admitted, _write_sizing

_FIXTURES = Path(__file__).parent / "fixtures"
_CALL_ARGS = re.compile(r"Workflow\(\{name: \"coordinator:engine-ask\", args: (\{.*\})\}\)")


@pytest.fixture
def repo(monkeypatch, tmp_path):
    content = tmp_path / "content"
    for name in ("pipeline_research", "pipeline_research_multi_repo"):
        shutil.copytree(_FIXTURES / name, content, dirs_exist_ok=True)
    work = tmp_path / "work"
    (work / ".git").mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: content)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-refire-gate")
    return work


def _sizing(repo: Path) -> str:
    flags = ["--research-class", "corpus", "--research-source", "repo"]
    for n in range(1, 6):
        flags += ["--research-question", f"Question {n}?"]
    return _write_sizing(repo, *flags)


def _call_sizing(call: str) -> str:
    match = _CALL_ARGS.fullmatch(call)
    assert match, call
    return json.loads(re.sub(r"(\{|, )(sizing):", r'\1"\2":', match.group(1)))["sizing"]


def _producers(repo: Path, rel: str) -> dict[str, str]:
    """Each halt-call producer's engine call string, keyed by producer."""
    envelope_call = f'Workflow({{name: "coordinator:engine-ask", args: {{"sizing": {json.dumps(rel)}}}}})'
    direct = halt_refire.refire_next_action(
        call=envelope_call, run_id="r-halted", sizing=rel, halted="dependents_unstarted"
    )
    wrapped = ask_contract.refire_next_action_for(
        ask_contract.HALT_DEPENDENTS_UNSTARTED, run_id="r-halted", sizing_rel=rel, detail="held"
    )
    fire = gate(repo, rel).halt
    assert fire["kind"] == "research" and len(fire["fire"]) == 1
    return {"refire_next_action": direct["call"], "refire_next_action_for": wrapped["call"], "ask_gate_fire": fire["fire"][0]}


@pytest.mark.parametrize("producer", ["refire_next_action", "refire_next_action_for", "ask_gate_fire"])
def test_halt_refire_call_passes_dispatch_emit_and_resolves_the_firing_repo(repo, producer):
    rel = _sizing(repo)
    call = _producers(repo, rel)[producer]
    assert _call_sizing(call) == rel

    params = {"session_id": "sess-refire-gate", "from_sizing": rel}
    ctx, _, _ = op_module._research_route_setup(params, repo, rel, None)
    repo_inputs = next(i for m, i, _ in ctx["segments"] if m.pipeline == "repo")
    assert f"repo target: {repo}" in (repo / repo_inputs.brief).read_text(encoding="utf-8")

    reply = op_module._dispatch_emit(params, repo)
    assert "error" not in reply and reply.get("path")
