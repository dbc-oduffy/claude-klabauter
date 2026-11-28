"""The composed run-close folds a UE-retrieval `run_rag_use` halted_by into `_halted` ahead of the commit stages."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, rag_use
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CAUSES = (
    rag_use.HALT_UE_RETRIEVAL_MISSING,
    rag_use.HALT_UE_RETRIEVAL_CHECK_FAILED,
    rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED,
)
_AGENTS = "[{ agent_id: 'a1', agent_type: 'coordinator:scout', label: 'scout-A', rag_calls: 2, ue_calls: 0 }]"
_RAG_USE = "{ agents: [], rag_calls: 2 }"


@pytest.fixture(scope="module")
def script():
    return _compose(in_run_commit=True)


def _line(script, prefix):
    return next(ln for ln in script.splitlines() if ln.lstrip().startswith(prefix))


def _run_close(script, reply, *, prior_halt="null"):
    """Run the composed fold, `_closeable` and the two commit-stage gates exactly as the default script carries them."""
    if shutil.which("node") is None:
        pytest.skip("needs node to evaluate the composed run close")
    closeable = _line(script, "const _closeable =")
    commit_gate = _line(script, "if (_closeable && _nextAction && _nextAction.kind === 'terminal_commit'")
    halt_gate = _line(script, "if (_halted && _RUN_TOKEN && !(_commit && _commit.committed === true))")
    harness = (
        "(async () => {\n"
        f"  let _halted = {prior_halt}, _runRagUse = null, _commit = null; const _runId = 'r1', _SESSION_ID = 's', _sizingRel = 'z.yaml';\n"
        "  const REPO_ROOT = '.', _RUN_TOKEN = 'tok', _manifest = {}, _deliveryVerdict = null, _viaRoadmap = false;\n"
        "  let _nextAction = { kind: 'terminal_commit' }; const log = () => {}; const ran = [];\n"
        f"  const engineOp = async (op, p) => {{ if (op !== 'dispatch.run_rag_use') throw new Error(op); return {reply}; }};\n"
        + ask_compose._run_rag_use_fold_js()
        + "\n  const _roadmapFan = !!(_viaRoadmap && !_halted && _nextAction && _nextAction.kind === 'terminal_commit');\n  "
        + closeable
        + "\n  "
        + commit_gate.rstrip().removesuffix("{") + "ran.push('commit');\n  "
        + halt_gate.rstrip().removesuffix("{") + "ran.push('halt-record');\n"
        "  console.log(JSON.stringify({ halted: _halted, ran, rag_use: _runRagUse }));\n})();"
    )
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(proc.stdout)


def _halt_reply(cause):
    return f"{{ rag_use: {_RAG_USE}, halted_by: {{ halted: {json.dumps(cause)}, cause: {json.dumps(cause)}, agents: {_AGENTS} }} }}"


def test_the_run_rag_use_call_precedes_closeable_and_both_commit_stages(script):
    call = script.index("dispatch.run_rag_use")
    assert call < script.index("const _closeable =")
    assert call < script.index("if (_closeable && _nextAction && _nextAction.kind === 'terminal_commit'")
    assert call < script.index("if (_halted && _RUN_TOKEN && !(_commit && _commit.committed === true))")
    assert script.count("dispatch.run_rag_use") == 1


@pytest.mark.parametrize("cause", CAUSES)
def test_a_ue_retrieval_cause_fails_the_run_naming_the_cause_with_no_product_commit(script, cause):
    out = _run_close(script, _halt_reply(cause))
    halted = out["halted"]
    assert halted["halted"] == cause and halted["reason"] == f"ue retrieval: {cause}"
    assert halted["goal_unmet"] is True and halted["run_id"] == "r1" and halted["agents"][0]["label"] == "scout-A"
    assert "commit" not in out["ran"] and out["ran"] == ["halt-record"]
    assert out["rag_use"] == {"agents": [], "rag_calls": 2}


def test_an_ok_reply_leaves_the_run_unhalted_and_the_product_commit_armed(script):
    out = _run_close(script, f"{{ rag_use: {_RAG_USE} }}")
    assert out["halted"] is None
    assert out["ran"] == ["commit"]
    assert out["rag_use"] == {"agents": [], "rag_calls": 2}


@pytest.mark.parametrize("cause", [rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED, rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_CADENCE])
def test_a_non_ue_retrieval_halted_by_is_not_folded(script, cause):
    out = _run_close(script, _halt_reply(cause))
    assert out["halted"] is None and out["ran"] == ["commit"]


def test_an_earlier_halt_is_never_overwritten_by_the_retrieval_cause(script):
    prior = "{ halted: 'review_blocked', run_id: 'r1' }"
    out = _run_close(script, _halt_reply(CAUSES[0]), prior_halt=prior)
    assert out["halted"] == {"halted": "review_blocked", "run_id": "r1"}


def test_the_returned_script_value_carries_halted_by_and_the_rag_use_receipt(script):
    assert re.search(r"halted_by: _halted, halt_commit: _haltCommit,\s*rag_degraded: _ragDegraded, rag_use: _runRagUse", script)
