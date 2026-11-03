"""Pins hooks.workflow_step_dispatch: registered, classified, no-match returns no_advisory without spawning."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core import ipc  # noqa: E402
from coordinator_core.hooks._envelope import no_advisory  # noqa: E402

_OP = "hooks.workflow_step_dispatch"


def test_registry_resolves_the_op_by_name_and_it_returns_no_advisory(monkeypatch):
    def _no_spawn(*a, **k):
        raise AssertionError("workflow_step_dispatch must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    handler = ipc.get_op_handler(_OP)
    assert handler is not None
    result = asyncio.run(handler({"hook_event_name": "SubagentStart"}, repo_root=None))
    assert result == no_advisory()


def test_both_subagent_events_return_the_same_no_match_result():
    handler = ipc.get_op_handler(_OP)
    for event in ("SubagentStart", "SubagentStop"):
        assert asyncio.run(handler({"hook_event_name": event}, repo_root="/repo")) == no_advisory()


def test_op_is_in_the_registry_map_and_classified():
    from coordinator_core.authz.classification import OpClass, OP_CLASSIFICATION
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert OP_MODULE_MAP[_OP] == "coordinator_core.hooks"
    assert OP_CLASSIFICATION[_OP] is OpClass.COMPUTE_ONLY
