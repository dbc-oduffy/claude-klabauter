"""Every executor dispatch route stages a brief or prompt carrying the declared-delete marker clause."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.bash_guards import _declared_delete_license
from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.ops.dispatch_emit import grind_stages
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

PLAN_REL = "docs/plans/p.md"
RUN = "r7"


def test_execute_plan_executor_briefs_carry_the_clause(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", True)
    (tmp_path / ".git").mkdir()
    rows = "".join(
        f"- id: {rid}\n  title: t\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
        f"  writes:\n    - docs/x/{rid}.md\n"
        for rid in ("C1", "C2")
    )
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )
    reply = read_stage_reply(tmp_path, _handler({"run_id": RUN, "plan_path": PLAN_REL}, repo_root=tmp_path))
    briefs = {r["id"]: (tmp_path / r["brief_path"]).read_text(encoding="utf-8") for r in reply["rows"]}
    assert set(briefs) == {"C1", "C2"}
    for rid, text in briefs.items():
        assert _declared_delete_license.brief_clause(RUN, rid) in text


def test_bug_blitz_fix_stage_prompt_carries_the_clause(monkeypatch) -> None:
    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", True)
    call = grind_stages.compose_fix_call(
        label="fix", phase_title="Grind", row_id_js="row.id", locked_files_js="files", run_id_js="RUN_ID"
    )
    assert _declared_delete_license.MARKER_VAR in call
    assert "COORDINATOR_DISPATCH_ROW=' + (RUN_ID) + '/' + (row.id)" in call


def test_bug_blitz_fix_stage_prompt_is_clause_free_while_the_channel_is_dormant(monkeypatch) -> None:
    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", False)
    call = grind_stages.compose_fix_call(
        label="fix", phase_title="Grind", row_id_js="row.id", locked_files_js="files", run_id_js="RUN_ID"
    )
    assert _declared_delete_license.MARKER_VAR not in call


def test_memo_draft_executor_brief_carries_the_clause(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit.ask_stage import OwedRow, _memo_draft_steps

    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", True)
    raw = {"id": "M1", "surface": ".coordinator-local/memo-outbox/topic.md", "memo_to": "other-em", "title": "T"}
    owed = [OwedRow(id="M1", reason="memo-send", detail="d")]
    run_dir = tmp_path / "runs" / RUN
    _, steps = _memo_draft_steps(
        tmp_path, run_dir, owed, {"M1": raw}, "", classify_receiver=lambda to, dry_run: None
    )
    assert [s.id for s in steps] == ["M1-memo-draft"]
    text = (tmp_path / steps[0].brief_path).read_text(encoding="utf-8")
    assert _declared_delete_license.brief_clause(RUN, "M1-memo-draft") in text


def test_memo_draft_executor_brief_is_clause_free_while_the_channel_is_dormant(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit.ask_stage import OwedRow, _memo_draft_steps

    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", False)

    raw = {"id": "M1", "surface": ".coordinator-local/memo-outbox/topic.md", "memo_to": "other-em", "title": "T"}
    owed = [OwedRow(id="M1", reason="memo-send", detail="d")]
    _, steps = _memo_draft_steps(
        tmp_path, tmp_path / "runs" / RUN, owed, {"M1": raw}, "", classify_receiver=lambda to, dry_run: None
    )
    text = (tmp_path / steps[0].brief_path).read_text(encoding="utf-8")
    assert _declared_delete_license.MARKER_VAR not in text
