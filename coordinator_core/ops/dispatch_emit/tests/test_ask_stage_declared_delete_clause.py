"""An executor row writing outside scratch/ carries the declared-delete marker line; a scratch-only row and a reviewer row do not."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.ops.dispatch_emit import falsifier_integrity_phase as fip
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

PLAN_REL = "docs/plans/p.md"
RUN = "r1"
MARKER = "COORDINATOR_DISPATCH_ROW=r1/"


def _stage(tmp_path: Path) -> dict:
    (tmp_path / ".git").mkdir(exist_ok=True)
    rows = (
        "- id: C1\n  title: t1\n  change_kind: doc-edit\n  surface: docs/x/C1.md\n"
        "  writes:\n    - docs/x/C1.md\n"
        "- id: C2\n  title: t2\n  change_kind: doc-edit\n  surface: scratch/old\n"
        "  writes:\n    - scratch/old\n"
    )
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )
    reply = read_stage_reply(tmp_path, _handler({"run_id": RUN, "plan_path": PLAN_REL}, repo_root=tmp_path))
    return {r["id"]: (tmp_path / r["brief_path"]).read_text(encoding="utf-8") for r in reply["rows"]}


def _channel_live(monkeypatch):
    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", True)


def test_executor_brief_carries_marker_with_run_and_row_id(tmp_path, monkeypatch):
    _channel_live(monkeypatch)
    briefs = _stage(tmp_path)
    assert MARKER + "C1 git rm" in briefs["C1"]


def test_dormant_channel_brief_has_no_marker(tmp_path):
    assert guard._DECLARED_DELETE_CHANNEL_LIVE is False
    briefs = _stage(tmp_path)
    assert "COORDINATOR_DISPATCH_ROW" not in briefs["C1"]


def test_scratch_only_row_gets_no_marker(tmp_path):
    briefs = _stage(tmp_path)
    assert "COORDINATOR_DISPATCH_ROW" not in briefs["C2"]


REVIEWED_REL = "docs/plans/reviewed.md"
_REVIEWED = (
    "---\ntitle: R\nprime_exit_criterion:\n  statement: the gate can fail\n  falsifier:\n"
    "    how: run tools/inst.py\n    baseline_output: green today\n    baseline_ref: abc1234\n"
    "    expected_when_true: red\n---\n# R\n"
)


def test_reviewer_brief_has_no_marker(tmp_path, tmp_path_factory, monkeypatch):
    rubric = tmp_path_factory.mktemp("rubric") / "falsifier-integrity-reviewer.md"
    rubric.write_text("rubric A\n", encoding="utf-8")
    monkeypatch.setattr(fip, "_reviewer_rubric_path", lambda: rubric)
    _channel_live(monkeypatch)
    (tmp_path / ".git").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "inst.py").write_text("print('ok')\n", encoding="utf-8", newline="\n")
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / REVIEWED_REL).write_text(_REVIEWED, encoding="utf-8", newline="\n")
    rows = (
        "- id: C1\n  title: t1\n  change_kind: doc-edit\n  surface: docs/x/C1.md\n"
        "  writes:\n    - docs/x/C1.md\n"
        "- id: S1\n  title: review\n  change_kind: verification\n"
        "  agent_type: coordinator:falsifier-integrity-reviewer\n"
        f"  falsifier_review_of: {REVIEWED_REL}\n  surface: {REVIEWED_REL}\n  writes: []\n"
    )
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )
    reply = read_stage_reply(tmp_path, _handler({"run_id": RUN, "plan_path": PLAN_REL}, repo_root=tmp_path))
    briefs = {r["id"]: (tmp_path / r["brief_path"]).read_text(encoding="utf-8") for r in reply["rows"]}
    assert MARKER + "C1 git rm" in briefs["C1"]
    assert "COORDINATOR_DISPATCH_ROW" not in briefs["S1"]
