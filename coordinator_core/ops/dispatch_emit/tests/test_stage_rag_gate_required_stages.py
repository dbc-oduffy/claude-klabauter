"""The composed default run aborts, with no terminal commit, on zero example-retrieval-repo calls at sizing, plan and review."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

pytestmark = pytest.mark.complete

REQUIRED_STAGES = ("sizing", "plan", "review")
_UNUSED = (
    "{ verdict: 'rag_not_used', tier: 'rag', rag_calls: 0, per_worker: [], halted_by: { halted: 'example_retrieval_repo_unused', "
    "agents: [{ agent_id: 'a1', agent_type: 'coordinator:scout', label: 'scout-A', rag_calls: 0 }] } }"
)
_USED = "{ verdict: 'ok', tier: 'rag', rag_calls: 3, per_worker: [] }"


def _run_gate(stage, reply):
    """Evaluate the gate snippet exactly as the composed script carries it, under node, over a stubbed engineOp reply."""
    if shutil.which("node") is None:
        pytest.skip("needs node to evaluate the composed stage gate")
    harness = (
        "(async () => {\n  let _halted = null; const _runId = 'r1', _SESSION_ID = 's', _sizingRel = 'z.yaml'; const log = () => {};\n"
        f"  const engineOp = async (op, p) => {{ if (op !== 'dispatch.stage_rag_gate' || p.stage !== {json.dumps(stage)}) throw new Error(op); return {reply}; }};\n"
        + ask_compose._stage_rag_gate_js(stage)
        + "\n  console.log(JSON.stringify(_halted));\n})();"
    )
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(proc.stdout)


def _flat(text):
    return "\n".join(line.strip() for line in text.splitlines())


@pytest.fixture(scope="module")
def default_run():
    return _flat(_compose(in_run_commit=True))


@pytest.mark.parametrize("stage", REQUIRED_STAGES)
def test_the_default_run_carries_each_required_stage_gate_once(default_run, stage):
    assert default_run.count(_flat(ask_compose._stage_rag_gate_js(stage))) == 1


def test_required_stage_gates_run_in_pipeline_order_before_the_next_stage(default_run):
    stages = re.findall(r"stage: '(\w+)' \}\); \}\ncatch \(e\) \{ log\('stage rag gate threw", default_run)
    assert [s for s in stages if s in REQUIRED_STAGES] == list(REQUIRED_STAGES)
    assert default_run.index("phase('gate')") > default_run.index("stage: 'sizing'")
    assert default_run.index("stage: 'plan'") < default_run.index("stage: 'build'") < default_run.index("stage: 'review'")


@pytest.mark.parametrize("stage", REQUIRED_STAGES)
def test_zero_example_retrieval_repo_calls_halt_the_run_at_the_stage(stage):
    halted = _run_gate(stage, _UNUSED)
    assert halted["halted"] == "example_retrieval_repo_unused"
    assert halted["reason"].startswith(f"{stage} stage:")
    assert stage in halted["next_action"]
    assert halted["agents"][0]["rag_calls"] == 0
    assert halted["run_id"] == "r1" and halted["sizing"] == "z.yaml"


@pytest.mark.parametrize("stage", REQUIRED_STAGES)
def test_a_stage_whose_agents_called_example_retrieval_repo_does_not_halt(stage):
    assert _run_gate(stage, _USED) is None


def test_a_halted_run_returns_halted_by_and_never_arms_the_terminal_commit(default_run):
    assert re.search(r"const _closeable = !!\(_roadmapFan \|\| \(!_halted && _manifest", default_run)
    assert "_closeable ? { kind: (_badIncompleteIds ? 'none' : 'terminal_commit')" in default_run
    assert "halted_by: _halted, halt_commit: _haltCommit" in default_run
    assert re.search(r"let _reviseAndRefire = _closeable \? null : _mkRevise\(\)", default_run)
