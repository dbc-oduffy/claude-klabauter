"""A staged row writing under scratch/ carries the scratch.delete call line; a row without one does not."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

PLAN_REL = "docs/plans/p.md"
RUN = "r1"


def _stage(tmp_path: Path) -> dict:
    (tmp_path / ".git").mkdir(exist_ok=True)
    rows = (
        "- id: C1\n  title: t1\n  change_kind: doc-edit\n  surface: scratch/old\n"
        "  writes:\n    - scratch/old\n    - docs/x/C1.md\n"
        "- id: C2\n  title: t2\n  change_kind: doc-edit\n  surface: docs/x/C2.md\n"
        "  writes:\n    - docs/x/C2.md\n"
    )
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )
    reply = read_stage_reply(tmp_path, _handler({"run_id": RUN, "plan_path": PLAN_REL}, repo_root=tmp_path))
    return {r["id"]: (tmp_path / r["brief_path"]).read_text(encoding="utf-8") for r in reply["rows"]}


def test_scratch_writer_brief_carries_call_line_and_other_row_does_not(tmp_path):
    briefs = _stage(tmp_path)
    assert "coordinator-invoke scratch.delete" in briefs["C1"]
    assert '"run_id": "r1"' in briefs["C1"]
    assert '"chunk_id": "C1"' in briefs["C1"]
    assert '["scratch/old"]' in briefs["C1"]
    assert "scratch.delete" not in briefs["C2"]
