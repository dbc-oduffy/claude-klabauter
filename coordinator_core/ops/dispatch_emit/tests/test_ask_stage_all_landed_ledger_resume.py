"""stage(ledger_resume=True) admits an all-coded spine as a zero-row manifest; the default still refuses."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, manifest_payload, stage

PLAN_REL = "docs/plans/p.md"
RUN = "r1"


def _row(rid: str) -> str:
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
        f"  writes:\n    - docs/x/{rid}.md\n  disposition: coded\n"
    )


def _plan(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{_row('A1')}{_row('A2')}```\n",
        encoding="utf-8",
        newline="\n",
    )


def test_ledger_resume_stages_a_zero_row_whole_manifest(tmp_path):
    _plan(tmp_path)
    manifest = stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL, ledger_resume=True)
    assert manifest.rows == ()
    assert manifest.run_dir and manifest.marker_path
    payload = manifest_payload(tmp_path, manifest)
    assert payload["rows"] == []
    assert "head_sha" in payload
    assert (tmp_path / manifest.run_dir).is_dir()


def test_handler_default_still_refuses_zero_dispatchable_rows(tmp_path):
    _plan(tmp_path)
    reply = ask_stage._handler({"run_id": RUN, "plan_path": PLAN_REL}, repo_root=tmp_path)
    assert "the spine derives zero dispatchable rows" in reply["error"]


def test_stage_default_raises_zero_dispatchable_rows(tmp_path):
    _plan(tmp_path)
    with pytest.raises(AskStageError, match="the spine derives zero dispatchable rows"):
        stage(tmp_path, run_id=RUN, plan_rel=PLAN_REL)
