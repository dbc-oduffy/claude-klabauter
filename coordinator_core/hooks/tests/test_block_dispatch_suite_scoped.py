"""Pins: the dispatch-prompt suite guard allows scoped invocations naming
explicit test files and runner names inside paths, and still denies broad ones."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from coordinator_core.hooks import block_dispatch_suite_invocation as bdsi

_ROOT = str(Path(__file__).resolve().parents[3])
_F = "coordinator_core/hooks/tests/test_nudge_em_code_dispatch.py"


def _decision(prompt: str) -> str:
    result = bdsi._handler(
        {"tool_name": "Agent", "tool_input": {"prompt": prompt}, "cwd": _ROOT}
    )
    return result.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


@pytest.mark.parametrize(
    "prompt",
    [
        f"Run `python -m pytest {_F} -q`",
        f"Run python -m pytest {_F} -q -p no:cacheprovider",
        f"Verify: python -m pytest {_F}::test_x -q",
        f"Run pytest {_F} coordinator_core/hooks/tests/test_stop_dispatch.py -q",
        f"Run python -m pytest -q {_F} -k 'a or b'",
        f"Run python -m pytest -m 'not slow' --tb=short {_F}",
        "Read pytest.ini, then edit it",
        "Run pytest.ini lint",
        "Run coordinator_core/pytest_plugins/test_a.py",
        "Run tests via C:/tools/pytest/bin/runner.py",
    ],
)
def test_scoped_invocation_or_path_naming_runner_is_allowed(prompt):
    assert _decision(prompt) == "allow"


@pytest.mark.parametrize(
    "prompt",
    [
        "Run pytest",
        "Run python -m pytest -q",
        "Run python -m pytest coordinator_core/ -q",
        "Run python -m pytest coordinator_core/hooks/tests -q",
    ],
)
def test_bare_or_broad_invocation_is_denied(prompt):
    assert _decision(prompt) == "deny"


def test_directory_target_deny_names_the_directory_and_the_node_id_remedy():
    """`pytest coordinator_core/hooks/tests` classifies as a suite command too; the generic suite reason
    dropped the directory (DoE test_precision_leg_denies_directory_scoped_imperative_brief)."""
    result = bdsi._handler(
        {"tool_name": "Agent", "tool_input": {"prompt": "Run: pytest coordinator_core/hooks/tests"}, "cwd": _ROOT}
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "coordinator_core/hooks/tests" in reason and "node ids" in reason


def test_suite_root_directory_deny_names_the_command_and_the_node_id_remedy():
    """A testpaths root is suite-shaped, so the precision leg skips it; the suite reason still names
    the directory (DoE test_precision_leg_denies_directory_scoped_imperative_brief)."""
    result = bdsi._handler({"tool_name": "Agent", "tool_input": {"prompt": "Run: pytest coordinator/tests"}, "cwd": _ROOT})
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "pytest coordinator/tests" in reason and "node ids" in reason


def test_engine_emitted_workflow_script_is_allowed_only_with_receipt(tmp_path):
    script = tmp_path / "run.workflow.js"
    script.write_text("agent(`\nRun python -m pytest -q\n`)\n", encoding="utf-8")
    tool_input = {
        "scriptPath": str(script),
        "args": {"sizing": "state/sizings/2026-10-23-band-dedupe-identity-partition-exemptions-8241be.yaml"},
    }
    event = {"tool_name": "Workflow", "tool_input": tool_input, "cwd": _ROOT}

    def decide():
        return bdsi._handler(event).get("hookSpecificOutput", {}).get("permissionDecision", "allow")

    assert decide() == "deny"
    receipt = {"sha256": hashlib.sha256(script.read_bytes()).hexdigest(), "plan": "p"}
    (tmp_path / "run.workflow.js.emitted.json").write_text(json.dumps(receipt), encoding="utf-8")
    assert decide() == "allow"


def test_the_guard_is_registered_box_degrading_so_its_deny_survives_the_policy_point():
    """Unregistered, apply_guard_level demotes every deny to a log: a Workflow agent running the full
    suite was allowed live (DoE report, 2026-10-29) while the tests above, at strict level, stayed green."""
    from coordinator_core.deny_registry import BOX_DEGRADING, GUARD_DENY_CLASS

    assert GUARD_DENY_CLASS.get(bdsi.GUARD_NAME) == BOX_DEGRADING
