"""A reviewer leg with a configured provider emits as an op-relay call to reviewer_run; others are unchanged."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.agent_pipe.reviewer_provider import OPUS_EQUIVALENT_MODELS, PROVIDER_ENV
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.predispatch import AgentSpec


def _spec(agent_type="coordinator:falsifier-integrity-reviewer"):
    return AgentSpec(
        key="plan.md",
        label="falsifier-integrity:1",
        phase="Review",
        agent_type=agent_type,
        model="sonnet",
        prompt="criterion: c",
        schema="falsifier_integrity_result",
    )


def _thunk(spec):
    return emit._spec_thunk_js(spec, None, None, None)


def test_unconfigured_reviewer_emits_the_claude_leg(monkeypatch):
    monkeypatch.delenv(PROVIDER_ENV, raising=False)
    js = _thunk(_spec())
    assert "agentType: 'coordinator:falsifier-integrity-reviewer'" in js
    assert "reviewer_run" not in js and "op-relay" not in js


def test_configured_reviewer_emits_an_op_relay_leg_on_the_mapped_model(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "coordinator:falsifier-integrity-reviewer=codex")
    js = _thunk(_spec())
    model = OPUS_EQUIVALENT_MODELS["codex"]
    assert "coordinator-invoke codex_runner.start" in js
    assert "coordinator-invoke codex_runner.result" in js
    assert "python -m" not in js
    assert f"provider: 'codex', model: '{model}'" in js
    assert 'verdicts: ["SOUND", "BROKEN", "UNREVIEWABLE"]' in js
    assert "op-relay" not in js and "agentType: 'general-purpose'" in js
    assert "label: 'falsifier-integrity:1'" in js and "phase: 'Review'" in js
    assert js.count("await agent(") == 1
    assert "criterion: c" in js


def test_configured_reviewer_runs_through_the_engine_when_the_runner_has_engine_op(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "coordinator:falsifier-integrity-reviewer=codex")
    js = _thunk(_spec())
    assert "typeof engineOp === 'function'" in js
    assert "engineOp('codex_runner.start', _request)" in js
    assert "engineOp('codex_runner.result', { run_id: _started.run_id })" in js
    assert js.index("engineOp('codex_runner.start'") < js.index("await agent(")


def test_wildcard_never_routes_a_non_reviewer_type(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=codex")
    js = _thunk(_spec("coordinator:executor"))
    assert "reviewer_run" not in js
    assert "agentType: 'coordinator:executor'" in js


def test_wildcard_routes_a_named_reviewer(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=agy")
    assert "codex_runner.start" in _thunk(_spec("coordinator:staff-eng"))


def test_cached_verdict_is_untouched_by_a_provider(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=codex")
    spec = _spec()._replace(schema=emit.CACHED_SCHEMA, prompt='{"verdict": "SOUND"}')
    assert _thunk(spec) == 'async () => ({"verdict": "SOUND"})'


def test_unknown_provider_refuses_rather_than_falling_back_to_claude(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=nonesuch")
    with pytest.raises(ValueError):
        _thunk(_spec())


_NODE = shutil.which("node")
_DRIVER = """
const thunk = %s;
globalThis.setTimeout = (fn) => fn();
const calls = [];
const results = [{status: 'running'}, {status: 'done', verdict: 'SOUND', findings: []}];
const engine = process.argv[1] === 'engine';
if (engine) globalThis.engineOp = async (op, p) => { calls.push(op); return op === 'codex_runner.start' ? {run_id: 'named-review-0123456789ab'} : results.shift(); };
const agent = async () => { calls.push('agent'); return {status: 'done', verdict: 'FALLBACK', findings: []}; };
thunk().then((r) => console.log(JSON.stringify({r, calls})));
"""


def _run_thunk(monkeypatch, mode):
    monkeypatch.setenv(PROVIDER_ENV, "coordinator:falsifier-integrity-reviewer=codex")
    src = _DRIVER % _thunk(_spec())
    out = subprocess.run(
        [_NODE, "-e", src, mode], capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_engine_op_runner_starts_polls_and_never_spawns_an_agent_leg(monkeypatch):
    got = _run_thunk(monkeypatch, "engine")
    assert got["calls"] == ["codex_runner.start", "codex_runner.result", "codex_runner.result"]
    assert got["r"]["verdict"] == "SOUND"


@pytest.mark.skipif(_NODE is None, reason="node not installed")
def test_runner_without_engine_op_falls_back_to_one_bounded_agent_leg(monkeypatch):
    got = _run_thunk(monkeypatch, "plain")
    assert got["calls"] == ["agent"]
    assert got["r"]["verdict"] == "FALLBACK"
    assert "at most 120 reads" in _thunk(_spec())
