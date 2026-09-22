"""Tests for state/bug-backlog/2026-08-28-awaiting-gate-is-read-by-nothing-
an-unde-de280708447e.yaml: `awaiting_gate` is a plausible-looking key that
plan-tasks.schema.json does not declare and no reader in this pipeline
consults. Read tolerantly (the pre-fix behaviour), a row carrying it and no
`external_gate` validates clean and dispatches exactly as though unblocked.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.spine_read import (
    UndeclaredGateKeyError,
    read_spine,
)

_HEADER = "# fixture plan\n\n## Tasks\n\n"


def _write_plan(tmp_path, body: str):
    path = tmp_path / "plan.md"
    path.write_text(_HEADER + "```yaml plan-tasks\n" + body + "\n```\n", encoding="utf-8")
    return path


def test_row_level_awaiting_gate_refuses_rather_than_dispatching(tmp_path):
    body = (
        "- id: C2\n"
        "  title: row with a plausible-looking but unread gate key\n"
        "  surface: some/surface\n"
        "  awaiting_gate: blocked on a doe-claude schema bump\n"
        "- id: C3\n"
        "  title: ordinary row\n"
        "  surface: some/other-surface\n"
    )
    plan_path = _write_plan(tmp_path, body)

    with pytest.raises(UndeclaredGateKeyError, match="C2"):
        read_spine(plan_path)


def test_row_without_awaiting_gate_is_unaffected(tmp_path):
    body = (
        "- id: C2\n"
        "  title: ordinary row\n"
        "  surface: some/surface\n"
    )
    plan_path = _write_plan(tmp_path, body)

    ids = {row.id for row in read_spine(plan_path)}

    assert ids == {"C2"}
