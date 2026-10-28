"""read_spine excludes a row whose epistemic-premise gate is recorded false; stamp_premise_closes closes it."""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.execute_plan_assemble.close_out_and_stamp import stamp_premise_closes
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.plan_tasks_mutate import evidence_sidecar_path

_C1 = (
    "- id: C1\n  title: probe\n  change_kind: doc-edit\n  surface: docs/x/c1.md\n"
    "  writes:\n    - docs/x/c1.md\n{disposition}"
)
_EDGE = "  depends_on:\n    - chunk: {dep}\n      gate_kind: {kind}\n      note: runs only if C1 records overlap yes\n"
_ROW = "- id: {id}\n  title: next\n  change_kind: doc-edit\n  surface: docs/x/{id}.md\n  writes:\n    - docs/x/{id}.md\n"


def _rows(c1_disposition: str = "coded") -> str:
    c1 = _C1.format(disposition=f"  disposition: {c1_disposition}\n" if c1_disposition != "open" else "")
    if c1_disposition == "coded":
        c1 += "  disposition_ref: abc1234\n"
    c2 = _ROW.format(id="C2") + _EDGE.format(dep="C1", kind="epistemic-premise")
    d3 = _ROW.format(id="D3") + _EDGE.format(dep="C2", kind="output-consumption-runtime")
    return c1 + c2 + d3


def _plan(tmp_path: Path, *, created: str = "2026-10-27", verdict=None, c1: str = "coded", premises_key=False) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / "docs/plans/p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\ncreated: {created}\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{_rows(c1)}```\n",
        encoding="utf-8",
        newline="\n",
    )
    if verdict or premises_key:
        entries = {"C1": {"verdict": verdict, "ref": "docs/x/c1.md", "recorded_at": "t"}} if verdict else {}
        evidence_sidecar_path(plan).write_text(yaml.safe_dump({"premises": entries}), encoding="utf-8", newline="\n")
    return plan


def _ids(plan: Path):
    ex: list = []
    rows = read_spine(plan, exclusions=ex)
    return [r.id for r in rows], {e["id"]: e for e in ex}


def test_premise_no_closes_row_and_blocks_its_dependents(tmp_path):
    dispatched, ex = _ids(_plan(tmp_path, verdict="no"))
    assert "C2" not in dispatched and "D3" not in dispatched
    assert ex["C2"]["reason"] == "premise-false"
    assert "C1" in ex["C2"]["detail"] and "no" in ex["C2"]["detail"] and "docs/x/c1.md" in ex["C2"]["detail"]
    assert ex["C2"]["premise_close"] == {"chunk": "C1", "verdict": "no", "ref": "docs/x/c1.md"}
    assert "D3" in ex


def test_premise_absent_closes_in_an_opted_in_plan(tmp_path):
    dispatched, ex = _ids(_plan(tmp_path))
    assert "C2" not in dispatched
    assert ex["C2"]["premise_close"]["verdict"] == "absent"


def test_premise_yes_dispatches(tmp_path):
    dispatched, ex = _ids(_plan(tmp_path, verdict="yes"))
    assert "C2" in dispatched
    assert "C2" not in ex


def test_legacy_plan_with_absent_verdict_still_dispatches(tmp_path):
    dispatched, ex = _ids(_plan(tmp_path, created="2026-09-01"))
    assert "C2" in dispatched
    assert "C2" not in ex


def test_legacy_plan_with_premises_key_is_opted_in(tmp_path):
    dispatched, ex = _ids(_plan(tmp_path, created="2026-09-01", premises_key=True))
    assert "C2" not in dispatched
    assert ex["C2"]["reason"] == "premise-false"


def test_open_premise_row_still_waits(tmp_path):
    dispatched, ex = _ids(_plan(tmp_path, c1="open"))
    assert dispatched[0] == "C1" and "C2" in dispatched
    assert not any(e["reason"] == "premise-false" for e in ex.values())


def test_read_spine_leaves_the_plan_bytes_unchanged(tmp_path):
    plan = _plan(tmp_path, verdict="no")
    before = plan.read_bytes()
    read_spine(plan, exclusions=[])
    assert plan.read_bytes() == before


def _stage(tmp_path: Path, plan: Path) -> dict:
    from coordinator_core.ops.dispatch_emit.ask_stage import _handler
    from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

    reply = read_stage_reply(tmp_path, _handler({"run_id": "r1", "plan_path": "docs/plans/p.md"}, repo_root=tmp_path))
    return {r["id"]: (tmp_path / r["brief_path"]).read_text(encoding="utf-8") for r in reply.get("rows", ())}


def test_stage_briefs_premise_row_with_record_line(tmp_path):
    from coordinator_core.ops.dispatch_emit.premise_gate import PREMISE_RECORD_BRIEF_LINE

    briefs = _stage(tmp_path, _plan(tmp_path, c1="open"))
    assert PREMISE_RECORD_BRIEF_LINE in briefs["C1"]
    assert PREMISE_RECORD_BRIEF_LINE not in briefs["C2"]


def test_stage_stamps_false_premise_row_wont_do_and_stages_no_executor_for_it(tmp_path):
    plan = _plan(tmp_path, verdict="no")
    briefs = _stage(tmp_path, plan)
    assert "C2" not in briefs and "D3" not in briefs
    body = plan.read_text(encoding="utf-8").split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    rows = {r["id"]: r for r in yaml.safe_load(body)}
    assert rows["C2"]["disposition"] == "wont_do" and rows["C2"]["premise_close"]["chunk"] == "C1"


def test_stamp_premise_closes_marks_wont_do_with_citation_and_is_idempotent(tmp_path):
    plan = _plan(tmp_path, verdict="no")
    cite = {"chunk": "C1", "verdict": "no", "ref": "docs/x/c1.md"}
    assert stamp_premise_closes(plan, {"C2": cite}, tmp_path) == ["C2"]
    text = plan.read_text(encoding="utf-8")
    body = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    rows = {r["id"]: r for r in yaml.safe_load(body)}
    assert rows["C2"]["disposition"] == "wont_do"
    assert rows["C2"]["premise_close"] == cite
    assert rows["C1"]["disposition"] == "coded"
    assert stamp_premise_closes(plan, {"C2": cite}, tmp_path) == []
