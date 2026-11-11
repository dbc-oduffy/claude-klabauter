"""`GATE_INDETERMINATE_RE` relabels a gate halt as a retry-safe timeout. A reason that says the error was
*not* a -32004 must not match (example-game-repo-19, 2026-11-11: a ModuleNotFoundError halt read "The gate timed out")."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import GATE_INDETERMINATE_RE


def _rx() -> re.Pattern:
    body, flags = GATE_INDETERMINATE_RE[1:].rsplit("/", 1)
    return re.compile(body, re.I if "i" in flags else 0)


@pytest.mark.parametrize("reason", [
    "-32004 on the second dispatch.ask_gate call: cold-run dispatch hung",
    "coordinator-invoke dispatch.ask_gate failed with JSON-RPC error -32004",
    "dispatch.ask_gate hung_past_ceiling after 150s",
])
def test_a_real_timeout_matches(reason):
    assert _rx().search(reason)


@pytest.mark.parametrize("reason", [
    "ModuleNotFoundError: No module named 'python_interp'. Not a -32004, so no retry was run.",
    "Internal error (-32603); not an -32004 timeout",
])
def test_a_negated_mention_does_not_match(reason):
    assert not _rx().search(reason)
