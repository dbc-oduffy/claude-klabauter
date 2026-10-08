"""Readers of the ``<plan-stem>.evidence.yaml`` row-evidence sidecar.

Pins the spine view (``render_row_evidence`` / ``spine_projection``) and the
terminal judge's brief (``compose_criterion_judge``'s ``row_evidence`` line).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.plan_tasks_render import (
    render_plan_row_evidence,
    render_row_evidence,
    spine_projection,
)

_PLAN = """\
---
title: "P"
status: ready
---

## Tasks

```yaml plan-tasks
- id: C1
  title: First
- id: C2
  title: Second
```
"""

_EVIDENCE = {"C1": [{"recorded_at": "2026-10-08T00:00:00Z", "text": "14 passed"}]}


def _seed(tmp_path: Path) -> Path:
    plan = tmp_path / "2026-10-08-p.md"
    plan.write_text(_PLAN, encoding="utf-8")
    plan.with_name("2026-10-08-p.evidence.yaml").write_text(
        yaml.safe_dump({"rows": _EVIDENCE}), encoding="utf-8"
    )
    return plan


def test_render_lists_only_rows_with_evidence():
    rows = [{"id": "C1", "title": "First"}, {"id": "C2", "title": "Second"}]
    out = render_row_evidence(rows, _EVIDENCE)
    assert "## Row evidence" in out
    assert "**C1**" in out and "14 passed" in out
    assert "C2" not in out
    assert render_row_evidence(rows, {}) == ""


def test_render_plan_reads_the_sidecar(tmp_path):
    plan = _seed(tmp_path)
    assert "14 passed" in render_plan_row_evidence(plan)
    plan.with_name("2026-10-08-p.evidence.yaml").unlink()
    assert render_plan_row_evidence(plan) == ""


def test_spine_projection_attaches_evidence_without_mutating_rows():
    rows = [{"id": "C1", "title": "First"}, {"id": "C2", "title": "Second"}]
    proj = spine_projection(rows, evidence=_EVIDENCE)
    by_id = {r["id"]: r for r in proj["open"]}
    assert by_id["C1"]["evidence"] == _EVIDENCE["C1"]
    assert "evidence" not in by_id["C2"]
    assert "evidence" not in rows[0]


def test_judge_prompt_carries_the_evidence_pointer_only_when_given():
    from coordinator_core.ops.review_mint.execute_review import compose_criterion_judge
    from coordinator_core.ops.review_mint.tests.test_execute_review import (
        _STAGE_SCHEMAS,
        _judge_review,
    )

    stage_schemas = {
        **_STAGE_SCHEMAS,
        "judge-result": {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]},
    }

    def compose(**kw):
        return compose_criterion_judge(
            _judge_review(),
            stage_schemas=stage_schemas,
            plan_path="docs/plans/example.md",
            run_base_sha="a" * 40,
            falsifier=None,
            **kw,
        )

    assert "row_evidence:" not in compose()
    assert "row_evidence: docs/plans/example.evidence.yaml" in compose(
        evidence_path="docs/plans/example.evidence.yaml"
    )
