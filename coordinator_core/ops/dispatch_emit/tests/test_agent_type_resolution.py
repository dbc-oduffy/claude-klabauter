"""Emit refuses a script naming a coordinator:* agentType the installed plugin lacks."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.emit import (
    UnresolvedAgentTypeError,
    check_agent_types_resolve,
)

_SCRIPT = (
    "await agent('p', { agentType: 'coordinator:executor' });\n"
    'await agent(\'j\', { agentType: "coordinator:exit-criterion-judge" });\n'
)


def _plugin(tmp_path, *names):
    agents = tmp_path / "plugin" / "agents"
    agents.mkdir(parents=True)
    for n in names:
        (agents / f"{n}.md").write_text("x", encoding="utf-8")
    return str(tmp_path / "plugin")


def test_refuses_when_a_roster_type_is_missing(tmp_path):
    root = _plugin(tmp_path, "executor")
    with pytest.raises(UnresolvedAgentTypeError) as exc:
        check_agent_types_resolve(_SCRIPT, claude_plugin_root=root, agent_type_host="coordinator")
    assert "coordinator:exit-criterion-judge" in str(exc.value)
    assert "coordinator:executor," not in str(exc.value)
    assert "agents" in str(exc.value)


def test_passes_when_every_type_is_present(tmp_path):
    root = _plugin(tmp_path, "executor", "exit-criterion-judge")
    check_agent_types_resolve(_SCRIPT, claude_plugin_root=root, agent_type_host="coordinator")


def test_skips_when_no_plugin_root():
    check_agent_types_resolve(_SCRIPT, claude_plugin_root=None, agent_type_host="coordinator")


def test_skips_when_agent_types_are_host_degraded(tmp_path):
    root = _plugin(tmp_path)
    check_agent_types_resolve(_SCRIPT, claude_plugin_root=root, agent_type_host="host")


def test_dispatch_emit_op_refuses_before_writing(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
    from coordinator_core.ops.dispatch_emit.tests.test_op import _write_fixture_plan

    root = _plugin(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", root)
    monkeypatch.delenv("COORDINATOR_AGENT_TYPE_HOST", raising=False)
    plan_path = _write_fixture_plan(tmp_path)
    out = tmp_path / "out" / "emitted.mjs"
    out.parent.mkdir()
    with pytest.raises(UnresolvedAgentTypeError):
        _dispatch_emit({"plan_path": str(plan_path), "output_path": str(out)})
    assert not out.exists()
