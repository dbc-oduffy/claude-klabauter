"""A large reverify test set reaches the runner as explicit batched paths, never stdin assembly."""

from __future__ import annotations

import re

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd


def _clause(n: int) -> str:
    tests = [f"coordinator_core/tests/test_tripwire_{i:03d}.py" for i in range(n)]
    return rd._scope_clause(tests, "", "abc123")


def test_175_paths_are_batched_explicit_and_forbid_stdin_assembly():
    clause = _clause(175)
    assert "never assemble a command from stdin (`xargs`" in clause
    assert "python -m pytest <paths> -q" in clause
    assert clause.count("[batch ") == 5
    assert "[batch 5/5]" in clause
    assert all(f"test_tripwire_{i:03d}.py" in clause for i in range(175))
    sizes = [len(b.split()) for b in re.findall(r"\[batch \d+/5\]((?: [\w/]+\.py)+)", clause)]
    assert sizes == [rd._BATCH_SIZE] * 4 + [175 - 4 * rd._BATCH_SIZE]


def test_small_set_stays_one_comma_list():
    clause = _clause(3)
    assert "[batch" not in clause
    assert "test_tripwire_000.py, coordinator_core/tests/test_tripwire_001.py" in clause
    assert "never assemble a command from stdin" in clause


def test_empty_set_runs_nothing_and_adds_no_command_guidance():
    clause = rd._scope_clause([], "", "abc123")
    assert "xargs" not in clause
