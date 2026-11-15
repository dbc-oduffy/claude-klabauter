"""Every ask gate leg, launcher child included, is typed with a shell-pinned, model-pinned agent type."""

from __future__ import annotations

import re

import pytest

from coordinator_core.codex_runner.workflow import _INVOKE_ONLY_AGENT_TYPES
from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_launcher_child_resume import _script

pytestmark = pytest.mark.complete

_GATE_CALL = re.compile(r"label: 'gate', phase: '[^']*', agentType: '([^']*)', model: '([^']*)'")


@pytest.mark.parametrize("launcher_child", [True, False])
@pytest.mark.parametrize("raw", [True, False])
def test_every_gate_leg_emits_the_pinned_gate_type(monkeypatch, launcher_child, raw):
    script = _script(monkeypatch, launcher_child=launcher_child, raw=raw)
    assert script.count("label: 'gate'") >= 1
    calls = _GATE_CALL.findall(script)
    assert len(calls) == script.count("label: 'gate'")
    for agent_type, model in calls:
        assert agent_type == ask_compose._GATE_AGENT_TYPE
        assert model == emit._AGENT_MODELS[ask_compose._GATE_AGENT_TYPE]


def test_the_gate_type_is_neither_unpinned_nor_invoke_only():
    gate = ask_compose._GATE_AGENT_TYPE
    assert gate != "general-purpose"
    assert gate in emit._AGENT_MODELS
    assert gate not in _INVOKE_ONLY_AGENT_TYPES
    assert ask_compose._MECHANICAL_MODEL == emit._AGENT_MODELS[gate]


def test_the_rulings_alias_stays_off_the_gate_type():
    assert ask_compose._RULINGS_AGENT_TYPE == ask_compose._MECHANICAL_AGENT_TYPE != ask_compose._GATE_AGENT_TYPE
