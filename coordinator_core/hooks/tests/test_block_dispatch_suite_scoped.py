"""Pins: the dispatch-prompt suite guard allows scoped invocations naming
explicit test files and runner names inside paths, and still denies broad ones."""

from __future__ import annotations

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
