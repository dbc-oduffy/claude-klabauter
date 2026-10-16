"""An EM discharge record satisfies an open performer: em row, so its dependents dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.plan_tasks_mutate import EM_DISCHARGE_KEY, evidence_sidecar_path

PLAN_REL = "docs/plans/p.md"

_DEP = "  depends_on:\n    - chunk: E1\n      gate_kind: output-consumption-runtime\n"


def _row(rid: str, extra: str = "") -> str:
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
        f"  writes:\n    - docs/x/{rid}.md\n{extra}"
    )


def _plan(tmp_path: Path, rows: str, discharged: tuple = ()) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
        newline="\n",
    )
    if discharged:
        entries = {rid: {"recorded_at": "2026-10-16T01:00:00Z", "text": "done"} for rid in discharged}
        evidence_sidecar_path(plan).write_text(
            yaml.safe_dump({EM_DISCHARGE_KEY: entries}), encoding="utf-8", newline="\n"
        )
    return plan


def _em_rows(e1_extra: str = "  performer: em\n") -> str:
    return _row("E1", e1_extra) + _row("C2", _DEP)


def test_discharged_em_row_releases_dependent(tmp_path):
    plan = _plan(tmp_path, _em_rows(), ("E1",))
    exclusions: list = []
    rows = read_spine(plan, exclusions=exclusions)
    assert [r.id for r in rows] == ["C2"]
    assert rows[0].depends_on == []
    assert [(e["id"], e["reason"]) for e in exclusions] == [("E1", "em-discharged")]
    assert "2026-10-16T01:00:00Z" in exclusions[0]["detail"]


def test_discharge_reaches_the_satisfied_split_without_an_exclusions_list(tmp_path):
    plan = _plan(tmp_path, _em_rows(), ("E1",))
    assert [r.id for r in read_spine(plan)] == ["C2"]


def test_undischarged_em_row_still_withholds_dependent(tmp_path):
    plan = _plan(tmp_path, _em_rows())
    exclusions: list = []
    assert read_spine(plan, exclusions=exclusions) == []
    assert [(e["id"], e["reason"]) for e in exclusions] == [
        ("E1", "em-performed"),
        ("C2", "withheld_by_owed_dependency"),
    ]
    assert "em-discharge" in exclusions[0]["detail"]


@pytest.mark.parametrize(
    "extra",
    [
        "  performer: em\n  kind: memo-send\n  receipt: .coordinator-local/memo-outbox/sent/slug-x.md\n",
        "  performer: em\n  execution_mode: operator\n",
    ],
    ids=["memo-send", "operator"],
)
def test_discharge_does_not_satisfy_memo_or_operator_rows(tmp_path, extra):
    plan = _plan(tmp_path, _em_rows(extra), ("E1",))
    exclusions: list = []
    assert read_spine(plan, exclusions=exclusions) == []
    assert "em-discharged" not in {e["reason"] for e in exclusions}


def test_uncleared_gate_beats_discharge(tmp_path):
    gate = "  performer: em\n  external_gate:\n    - owner_repo: r\n      condition: c\n"
    plan = _plan(tmp_path, _em_rows(gate), ("E1",))
    exclusions: list = []
    assert read_spine(plan, exclusions=exclusions) == []
    assert exclusions[0]["reason"] == "external_gate"


def test_resume_after_discharge_stages_dependent_and_knows_discharged_row(tmp_path):
    _plan(tmp_path, _em_rows(), ("E1",))
    reply = _handler({"run_id": "r1", "plan_path": PLAN_REL, "only_rows": ["E1", "C2"]}, repo_root=tmp_path)
    assert "error" not in reply
    assert [r["id"] for r in reply["rows"]] == ["C2"]
