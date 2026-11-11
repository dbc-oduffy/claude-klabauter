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


def test_degraded_host_skips_the_plugin_stat_for_a_script_with_no_coordinator_type(tmp_path):
    root = _plugin(tmp_path)
    check_agent_types_resolve(
        "await agent('p', { agentType: 'general-purpose' });\n",
        claude_plugin_root=root,
        agent_type_host="host",
    )


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


_STEM = "ue-metahuman-likeness-specialist"


def _home_with_plugins(tmp_path, monkeypatch, *plugins):
    """A fake home whose `.claude/plugins/<plugin>/agents/<stem>.md` exists for each plugin."""
    home = tmp_path / "home"
    for plugin in plugins:
        agents = home / ".claude" / "plugins" / plugin / "agents"
        agents.mkdir(parents=True)
        (agents / f"{_STEM}.md").write_text("---\nname: x\nmodel: sonnet\n---\nbody\n", encoding="utf-8")
    from coordinator_core.hooks import block_unenumerated_agent_type as hook

    home.mkdir(exist_ok=True)
    monkeypatch.setattr(hook, "_home_dir", lambda: str(home))
    return home


def _emit_bare_row(tmp_path, monkeypatch, agent_type):
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
    from coordinator_core.ops.dispatch_emit.tests.test_op import _FIXTURE_PLAN, _bare_host

    _bare_host(tmp_path, monkeypatch)
    text = _FIXTURE_PLAN.replace(
        "  reads: []\n  queue_scope", f"  reads: []\n  agent_type: {agent_type}\n  queue_scope", 1
    )
    assert f"agent_type: {agent_type}" in text
    plan_path = tmp_path / "bare-plan.md"
    plan_path.write_text(text, encoding="utf-8")
    out = tmp_path / "out" / "emitted.mjs"
    out.parent.mkdir()
    _dispatch_emit({"plan_path": str(plan_path), "output_path": str(out)})
    return out.read_text(encoding="utf-8")


def _row(agent_type):
    from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

    return WaveRow("C1", "t", "s", ("x.py",), [], [], agent_type=agent_type)


def test_emit_qualifies_a_bare_type_naming_one_installed_plugin_agent(tmp_path, monkeypatch):
    _home_with_plugins(tmp_path, monkeypatch, "example-game-repo-control")
    script = _emit_bare_row(tmp_path, monkeypatch, _STEM)
    assert f"example-game-repo-control:{_STEM}" in script


def test_emit_refuses_an_unknown_bare_type_by_name(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.emit import UnresolvableBareAgentTypeError

    _home_with_plugins(tmp_path, monkeypatch)
    with pytest.raises(UnresolvableBareAgentTypeError, match=_STEM):
        _emit_bare_row(tmp_path, monkeypatch, _STEM)
    assert not (tmp_path / "out" / "emitted.mjs").exists()


def test_emit_refuses_an_ambiguous_bare_type_naming_every_candidate(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.emit import UnresolvableBareAgentTypeError

    _home_with_plugins(tmp_path, monkeypatch, "example-game-repo-control", "example-game-repo")
    with pytest.raises(UnresolvableBareAgentTypeError) as exc:
        _emit_bare_row(tmp_path, monkeypatch, _STEM)
    msg = str(exc.value)
    assert _STEM in msg and f"example-game-repo-control:{_STEM}" in msg and f"example-game-repo:{_STEM}" in msg


def test_row_agent_type_leaves_qualified_and_builtin_types_unchanged(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import emit

    _home_with_plugins(tmp_path, monkeypatch, "example-game-repo-control", "example-game-repo")
    for kept in (f"example-game-repo:{_STEM}", "general-purpose", "Explore", "coordinator:executor"):
        assert emit._row_agent_type(_row(kept)) == kept


def test_refusal_is_a_routing_error_so_prep_gate_reports_the_row(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import emit
    from coordinator_core.roadmap.prep_gate import _unroutable_rows

    _home_with_plugins(tmp_path, monkeypatch, "example-game-repo-control")
    assert emit.UnresolvableBareAgentTypeError in emit.ROW_ROUTING_ERRORS
    ids, first = _unroutable_rows([[_row("no-such-agent")]])
    assert ids == ["C1"] and "no-such-agent" in str(first)
    ids, first = _unroutable_rows([[_row(_STEM)]])
    assert ids == [] and first is None


def test_qualified_type_in_the_full_roster_but_not_the_plugin_leg_passes(tmp_path, monkeypatch):
    from coordinator_core.hooks import block_unenumerated_agent_type as hook
    from coordinator_core.ops.dispatch_emit import emit

    _home_with_plugins(tmp_path, monkeypatch, "example-game-repo-control")
    only_in_agents = "coordinator:agents-dir-only"
    monkeypatch.setattr(hook, "resolve_roster", lambda: (frozenset({only_in_agents}), None))
    assert emit._row_agent_type(_row(only_in_agents)) == only_in_agents
    with pytest.raises(emit.UnresolvableQualifiedAgentTypeError):
        emit._row_agent_type(_row("coordinator:not-anywhere"))
