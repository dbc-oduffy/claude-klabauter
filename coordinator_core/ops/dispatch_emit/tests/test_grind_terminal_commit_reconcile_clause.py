"""The composed grind script's terminal-commit prompt carries the terminal-commit reconcile clause once."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.terminal_commit_reconcile import (
    TERMINAL_COMMIT_RECONCILE_CLAUSE,
)

from .test_grind_compose import _compose


def _js_escaped(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'")


def test_terminal_commit_prompt_carries_clause_exactly_once():
    script = _compose()
    assert script.count(_js_escaped(TERMINAL_COMMIT_RECONCILE_CLAUSE)) == 1
