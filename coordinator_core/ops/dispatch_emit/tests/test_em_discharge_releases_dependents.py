"""A cited em-discharge closes an executor row for the spine readers; an uncited entry does not."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.ask_gate import _discharged_em_items
from coordinator_core.ops.dispatch_emit.spine_read import drop_em_discharged, load_rows_memo, read_spine
from coordinator_core.ops.plan_tasks_mutate import EM_DISCHARGE_KEY, evidence_sidecar_path

_ROWS = (
    "- id: B1\n  title: review\n  change_kind: doc-edit\n  surface: docs/x/b1.md\n"
    "  writes:\n    - docs/x/b1.md\n  agent_type: coordinator:executor\n  disposition: open\n"
    "- id: C2\n  title: next\n  change_kind: doc-edit\n  surface: docs/x/c2.md\n"
    "  writes:\n    - docs/x/c2.md\n  depends_on:\n    - chunk: B1\n      gate_kind: output-consumption-runtime\n"
)
_CITED = {"recorded_at": "2026-11-11T01:00:00Z", "text": "ran", "commit": "a" * 40, "evidence": "vitest 69/69"}
_LEGACY = {"recorded_at": "2026-11-11T01:00:00Z", "text": "ran"}


def _plan(tmp_path: Path, entry: dict | None) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / "docs/plans/p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{_ROWS}```\n", encoding="utf-8", newline="\n"
    )
    if entry is not None:
        evidence_sidecar_path(plan).write_text(
            yaml.safe_dump({EM_DISCHARGE_KEY: {"B1": entry}}), encoding="utf-8", newline="\n"
        )
    return plan


def test_executor_row_with_a_cited_entry_drops_and_releases_its_dependent(tmp_path):
    plan = _plan(tmp_path, _CITED)
    text = plan.read_text(encoding="utf-8")
    rows = list(load_rows_memo(text).rows)
    assert drop_em_discharged(plan, text, rows, ["B1", "C2"]) == ["C2"]

    exclusions: list = []
    spine = read_spine(plan, exclusions=exclusions)
    assert [r.id for r in spine] == ["C2"]
    assert spine[0].depends_on == []
    assert [(e["id"], e["reason"]) for e in exclusions] == [("B1", "em-discharged")]


def test_executor_row_with_an_uncited_legacy_entry_stays_open(tmp_path):
    plan = _plan(tmp_path, _LEGACY)
    text = plan.read_text(encoding="utf-8")
    rows = list(load_rows_memo(text).rows)
    assert drop_em_discharged(plan, text, rows, ["B1", "C2"]) == ["B1", "C2"]
    assert "B1" in [r.id for r in read_spine(plan)]


def test_executor_row_without_any_entry_stays_open(tmp_path):
    plan = _plan(tmp_path, None)
    text = plan.read_text(encoding="utf-8")
    assert drop_em_discharged(plan, text, list(load_rows_memo(text).rows), ["B1"]) == ["B1"]


def test_executor_item_is_credited_only_by_a_cited_entry(tmp_path):
    plan = _plan(tmp_path, _CITED)
    serving = [{"id": "B1", "writes": ["docs/x/b1.md"]}]
    from unittest import mock

    with mock.patch(
        "coordinator_core.ops.dispatch_emit.ledger_remaining_paths.serving_rows", return_value=serving
    ):
        assert _discharged_em_items(["item"], {"B1": _CITED}, plan) == {"item"}
        assert _discharged_em_items(["item"], {"B1": _LEGACY}, plan) == set()
