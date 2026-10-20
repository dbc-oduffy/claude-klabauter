"""read_spine and em_discharge_eligible derive em-performance from a row's agent-forbidden sole act."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.ops.dispatch_emit.spine_read import em_discharge_eligible, read_spine
from coordinator_core.ops.plan_tasks_mutate import EM_DISCHARGE_KEY, evidence_sidecar_path

_CMD = "pwsh -NoProfile -File scripts/build-plugin.ps1 -EngineVersion 5.8"
_DEP = "  depends_on:\n    - chunk: B1\n      gate_kind: output-consumption-runtime\n"


def _b1(writes: str = "  writes: []\n", disposition: str = "") -> str:
    return (
        "- id: B1\n  title: build\n  change_kind: verification\n  surface: scripts/build-plugin.ps1\n"
        f"{writes}  agent_type: coordinator:executor\n{disposition}"
        f"  body: |\n    Run `{_CMD}`.\n"
    )


_DEPENDENT = (
    "- id: C2\n  title: next\n  change_kind: doc-edit\n  surface: docs/x/c2.md\n"
    f"  writes:\n    - docs/x/c2.md\n{_DEP}"
)


def _plan(tmp_path: Path, rows: str, discharged: tuple = ()) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / "docs/plans/p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
        newline="\n",
    )
    if discharged:
        entries = {rid: {"recorded_at": "2026-10-18T01:00:00Z", "text": "ran"} for rid in discharged}
        evidence_sidecar_path(plan).write_text(
            yaml.safe_dump({EM_DISCHARGE_KEY: entries}), encoding="utf-8", newline="\n"
        )
    return plan


def test_forbidden_sole_act_row_is_excluded_em_performed_with_span(tmp_path):
    exclusions: list = []
    rows = read_spine(_plan(tmp_path, _b1()), exclusions=exclusions)
    assert rows == []
    (entry,) = exclusions
    assert (entry["id"], entry["reason"]) == ("B1", "em-performed")
    assert f"`{_CMD}`" in entry["detail"]
    assert "em-discharge" in entry["detail"]


def test_dependent_held_until_discharge_then_released(tmp_path):
    held: list = []
    assert read_spine(_plan(tmp_path, _b1() + _DEPENDENT), exclusions=held) == []
    assert {e["id"] for e in held} == {"B1", "C2"}

    released: list = []
    rows = read_spine(_plan(tmp_path, _b1() + _DEPENDENT, ("B1",)), exclusions=released)
    assert [r.id for r in rows] == ["C2"]
    assert rows[0].depends_on == []
    assert [(e["id"], e["reason"]) for e in released] == [("B1", "em-discharged")]


def test_declared_em_row_keeps_plain_detail(tmp_path):
    rows = _b1(writes="  writes: []\n  performer: em\n")
    exclusions: list = []
    read_spine(_plan(tmp_path, rows), exclusions=exclusions)
    assert exclusions[0]["detail"].startswith("EM: perform it, then run")


def test_row_with_a_write_act_and_the_same_body_dispatches(tmp_path):
    rows = read_spine(_plan(tmp_path, _b1(writes="  writes:\n    - src/a.cpp\n")))
    assert [r.id for r in rows] == ["B1"]


def test_eligibility_follows_the_derivation_and_the_disposition(tmp_path):
    raw = {"id": "B1", "writes": [], "body": f"`{_CMD}`"}
    assert em_discharge_eligible(raw) is True
    assert em_discharge_eligible({**raw, "disposition": "coded"}) is False
    assert em_discharge_eligible({**raw, "writes": ["a.cpp"]}) is False


def test_row_blocked_on_the_workflow_tool_is_excluded_em_performed(tmp_path):
    rows = (
        "- id: W1\n  title: fire\n  change_kind: code-edit\n  surface: pkg/a.py\n"
        "  writes:\n    - pkg/a.py\n  agent_type: coordinator:executor\n"
        "  blocked_on:\n    capability: Workflow tool\n    probe: 'ToolSearch select:Workflow'\n"
        "  body: |\n    Fire the workflow.\n"
    )
    exclusions: list = []
    assert read_spine(_plan(tmp_path, rows), exclusions=exclusions) == []
    (entry,) = exclusions
    assert (entry["id"], entry["reason"]) == ("W1", "em-performed")
    assert "Workflow tool" in entry["detail"]
