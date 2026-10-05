"""A host-degraded emit refuses a script that still names a coordinator: agentType."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.emit import (
    UnresolvedAgentTypeError,
    check_agent_types_resolve,
)


def test_degraded_host_refuses_survivors_and_counts_them():
    script = (
        "agent('a', { agentType: 'coordinator:apm' });\n"
        "agent('b', { agentType: \"coordinator:apm\" });\n"
        "agent('c', { agentType: 'general-purpose' });\n"
    )
    with pytest.raises(UnresolvedAgentTypeError) as exc:
        check_agent_types_resolve(script, claude_plugin_root=None, agent_type_host="host")
    assert "coordinator:apm (2x)" in str(exc.value)
    assert "general-purpose" not in str(exc.value)


def test_degraded_host_passes_when_nothing_survives():
    check_agent_types_resolve(
        "agent('c', { agentType: 'general-purpose' });\n",
        claude_plugin_root=None,
        agent_type_host="host",
    )


def test_coordinator_host_still_admits_a_coordinator_type_with_no_plugin_root():
    check_agent_types_resolve(
        "agent('a', { agentType: 'coordinator:apm' });\n",
        claude_plugin_root=None,
        agent_type_host="coordinator",
    )
