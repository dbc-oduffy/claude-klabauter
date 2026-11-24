"""The targeted-test-run command a bug-blitz stage agent is told to issue is admitted to a subagent.

Spec backlink: coordinator_core/bash_guards/check_test_suite_invocation.py (check),
coordinator_core/ops/dispatch_emit/grind_stages.py (_runner_invocation_parts).
A denial here is a guard change routed to the EM, never a loosening of the pytest refusal.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.ops.dispatch_emit import grind_stages

_REPO = str(Path(__file__).resolve().parents[3])
_TOUCHED = "coordinator_core/bug_blitz/contract.py"
_ROW = "docs/plans/2026-11/some-plan.md"
_RUN_ID = "run-1"
_ROW_ID = "C1"


def _runner_command(*, committed_head: bool) -> str:
    """The command text grind_stages' own builder spells, with its runtime parts bound."""
    parts = grind_stages._runner_invocation_parts(
        profile="bug-blitz",
        row_path_part=("lit", shlex.quote(_ROW)),
        touched_parts=[("lit", shlex.quote(_TOUCHED))],
        run_id_part=("lit", _RUN_ID),
        row_id_part=("lit", _ROW_ID),
        repo_root_js="REPO",
        committed_head=committed_head,
    )
    text = "".join(
        shlex.quote(_REPO) if t == "_shq(REPO)" else t for _, t in parts
    )
    assert text.startswith("`") and text.endswith("`")
    return text[1:-1]


def _verdict(cmd: str, *, subagent: bool):
    payload = {
        "tool_name": "Bash",
        "session_id": "s",
        "cwd": _REPO,
        "tool_input": {"command": cmd},
    }
    if subagent:
        payload["agent_id"] = "agent-1"
    res = guard.check(payload)
    return None if res is None else res.verdict_kind


@pytest.mark.parametrize("committed_head", [False, True])
def test_stage_agent_runner_command_is_admitted_for_a_subagent(committed_head):
    cmd = _runner_command(committed_head=committed_head)
    assert "targeted-test-run" in cmd
    assert _verdict(cmd, subagent=True) != "deny"


def test_a_bare_pytest_is_still_refused_to_the_same_subagent():
    assert _verdict("pytest", subagent=True) == "deny"
