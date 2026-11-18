"""A ledger resume with no halted predecessor still briefs the run's own live revise notes."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_stage import stage

PLAN_REL = "docs/plans/p.md"
RUN = "r1"
NOTE = "also add the targeted_test_launcher key"


def _plan(root: Path) -> None:
    (root / ".git").mkdir(exist_ok=True)
    plan = root / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    row = (
        "- id: A1\n  title: tA1\n  change_kind: doc-edit\n  surface: docs/x/A1.md\n"
        "  writes:\n    - docs/x/A1.md\n"
    )
    plan.write_text(f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{row}```\n", encoding="utf-8", newline="\n")


def _brief(root: Path, **kw) -> str:
    m = stage(root, run_id=RUN, plan_rel=PLAN_REL, only_rows=["A1"], **kw)
    return (root / m.run_dir / "briefs" / "A1.md").read_text(encoding="utf-8")


def test_ledger_resume_without_a_prior_run_briefs_the_runs_own_live_note(tmp_path):
    _plan(tmp_path)
    rl.start(tmp_path, RUN)
    (rl.run_dir(tmp_path, RUN) / rl.NOTES_FILE).write_text(
        json.dumps({"note": NOTE, "at": "2026-10-21T23:50:00Z"}) + "\n", encoding="utf-8"
    )
    assert NOTE in _brief(tmp_path, ledger_resume=True)


def test_a_plain_stage_without_a_prior_run_reads_no_notes(tmp_path):
    _plan(tmp_path)
    rl.start(tmp_path, RUN)
    (rl.run_dir(tmp_path, RUN) / rl.NOTES_FILE).write_text(
        json.dumps({"note": NOTE, "at": "2026-10-21T23:50:00Z"}) + "\n", encoding="utf-8"
    )
    assert NOTE not in _brief(tmp_path)
