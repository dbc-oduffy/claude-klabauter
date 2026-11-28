"""A row naming a non-claude family dispatches to the family runner even when marked em_run."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.ops.dispatch_emit.spine_read import InvalidFieldTypeError, read_spine


def _plan(tmp_path: Path, extra: str) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / "docs/plans/p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    rows = (
        "- id: F1\n  title: review it\n  change_kind: doc-edit\n  surface: docs/x/f1.md\n"
        f"  writes:\n    - docs/x/f1.md\n{extra}  body: |\n    Review the doc.\n"
    )
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
        newline="\n",
    )
    return plan


def test_em_run_codex_row_dispatches_to_the_family_runner(tmp_path):
    exclusions: list = []
    rows = read_spine(_plan(tmp_path, "  em_run: true\n  family: codex\n"), exclusions=exclusions)
    assert [r.id for r in rows] == ["F1"]
    assert rows[0].agent_type == stage_family.RUNNER_AGENT
    assert "family: codex" in rows[0].body
    assert exclusions == []


@pytest.mark.parametrize("extra", ["  em_run: true\n", "  em_run: true\n  family: claude\n"])
def test_em_run_row_without_a_non_claude_family_stays_em_owed(tmp_path, extra):
    exclusions: list = []
    assert read_spine(_plan(tmp_path, extra), exclusions=exclusions) == []
    assert [(e["id"], e["reason"]) for e in exclusions] == [("F1", "em-performed")]


def test_em_run_row_with_an_unknown_family_is_refused_not_excluded(tmp_path):
    with pytest.raises(InvalidFieldTypeError, match="unknown stage family"):
        read_spine(_plan(tmp_path, "  em_run: true\n  family: gpt\n"), exclusions=[])
