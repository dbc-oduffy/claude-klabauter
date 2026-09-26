"""
Tests for host agent-type degradation (S1-C5).

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, row S1-C5.

Covers the ``resolve_agent_type_host`` ladder (explicit
``COORDINATOR_AGENT_TYPE_HOST``, then ``CLAUDE_PLUGIN_ROOT``, then this
session's coordinator hook state, then the default-degrade) and
``compose_script``'s consumption of the resolved value: every emitted
``agentType`` literal substitutes through the host-native roster on
degrade, while every ``model:`` literal is untouched.
"""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit.emit import (
    _AGENT_TYPE_HOST_COORDINATOR,
    _AGENT_TYPE_HOST_DEGRADED,
    _COMMIT_AGENT_TYPE,
    _ENRICHER_AGENT_TYPE,
    _EXECUTOR_AGENT_TYPE,
    _TEST_AGENT_TYPE,
    _degrade_agent_type,
    compose_script,
    resolve_agent_type_host,
)
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

_MODEL_LITERAL_RE = re.compile(r"model:\s*'([^']*)'")
_AGENT_TYPE_LITERAL_RE = re.compile(r"agentType:\s*'([^']*)'")


def _wave_row(id_, writes, agent_type=None, agent_model=None):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface="dispatch_emit",
        writes=writes,
        reads=[],
        depends_on=[],
        agent_type=agent_type,
        agent_model=agent_model,
    )


def _one_wave_fixture():
    return [[_wave_row("C1", ["coordinator_core/ops/dispatch_emit/spine_read.py"])]]


def test_rung1_explicit_env_var_wins_over_everything():
    assert (
        resolve_agent_type_host(
            coordinator_agent_type_host="coordinator",
            claude_plugin_root=None,
            coordinator_hook_state=False,
        )
        == "coordinator"
    )
    assert (
        resolve_agent_type_host(
            coordinator_agent_type_host="host",
            claude_plugin_root="/some/plugin/root",
            coordinator_hook_state=True,
        )
        == "host"
    )


def test_rung2_claude_plugin_root_resolves_coordinator_absent_env_var():
    assert (
        resolve_agent_type_host(
            coordinator_agent_type_host=None,
            claude_plugin_root="/some/plugin/root",
            coordinator_hook_state=False,
        )
        == _AGENT_TYPE_HOST_COORDINATOR
    )


def test_rung3_coordinator_hook_state_resolves_coordinator_absent_env_and_plugin_root():
    assert (
        resolve_agent_type_host(
            coordinator_agent_type_host=None,
            claude_plugin_root=None,
            coordinator_hook_state=True,
        )
        == _AGENT_TYPE_HOST_COORDINATOR
    )


def test_default_degrade_when_no_rung_resolves():
    assert (
        resolve_agent_type_host(
            coordinator_agent_type_host=None,
            claude_plugin_root=None,
            coordinator_hook_state=False,
        )
        == _AGENT_TYPE_HOST_DEGRADED
    )


def test_resolve_agent_type_host_reads_no_environment_itself(monkeypatch):
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/somewhere")
    assert (
        resolve_agent_type_host(
            coordinator_agent_type_host=None,
            claude_plugin_root=None,
            coordinator_hook_state=False,
        )
        == _AGENT_TYPE_HOST_DEGRADED
    )


def test_degrade_agent_type_unchanged_when_not_degraded():
    assert _degrade_agent_type(_EXECUTOR_AGENT_TYPE, None) == _EXECUTOR_AGENT_TYPE
    assert (
        _degrade_agent_type(_EXECUTOR_AGENT_TYPE, _AGENT_TYPE_HOST_COORDINATOR)
        == _EXECUTOR_AGENT_TYPE
    )


def test_degrade_agent_type_substitutes_known_types_on_degrade():
    for agent_type in (
        _EXECUTOR_AGENT_TYPE,
        _ENRICHER_AGENT_TYPE,
        _COMMIT_AGENT_TYPE,
        _TEST_AGENT_TYPE,
    ):
        assert _degrade_agent_type(agent_type, _AGENT_TYPE_HOST_DEGRADED) == "general-purpose"


def test_degrade_agent_type_leaves_unregistered_type_unchanged():
    assert (
        _degrade_agent_type("coordinator:workflow-maker", _AGENT_TYPE_HOST_DEGRADED)
        == "coordinator:workflow-maker"
    )


def test_compose_script_leaves_agent_types_untouched_absent_agent_type_host():
    waves = _one_wave_fixture()
    script = compose_script(waves, name="wf", description="one wave")

    assert f"agentType: '{_EXECUTOR_AGENT_TYPE}'" in script
    assert f"agentType: '{_COMMIT_AGENT_TYPE}'" in script
    assert "general-purpose" not in script


def test_compose_script_degrades_every_emitted_agent_type_on_host():
    waves = _one_wave_fixture()
    script = compose_script(
        waves, name="wf", description="one wave", agent_type_host=_AGENT_TYPE_HOST_DEGRADED
    )

    agent_types = set(_AGENT_TYPE_LITERAL_RE.findall(script))
    assert agent_types == {"general-purpose"}
    assert _EXECUTOR_AGENT_TYPE not in script
    assert _COMMIT_AGENT_TYPE not in script


def test_compose_script_degraded_narrates_the_loss():
    waves = _one_wave_fixture()
    script = compose_script(
        waves, name="wf", description="one wave", agent_type_host=_AGENT_TYPE_HOST_DEGRADED
    )

    assert "Agent-type host degradation" in script
    assert "general-purpose" in script


def test_compose_script_not_degraded_emits_no_narration():
    waves = _one_wave_fixture()
    script = compose_script(waves, name="wf", description="one wave")

    assert "Agent-type host degradation" not in script


def test_compose_script_degrade_never_touches_model_literal():
    waves = _one_wave_fixture()
    baseline = compose_script(waves, name="wf", description="one wave")
    degraded = compose_script(
        waves, name="wf", description="one wave", agent_type_host=_AGENT_TYPE_HOST_DEGRADED
    )

    baseline_models = _MODEL_LITERAL_RE.findall(baseline)
    degraded_models = _MODEL_LITERAL_RE.findall(degraded)
    assert baseline_models == degraded_models
    assert baseline_models
