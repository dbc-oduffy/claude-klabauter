"""A family stage's output dir is run-scoped and its brief demands a fresh invoke."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import stage_family as sf


def test_two_runs_get_distinct_dirs():
    a = sf.family_scratch_dir("C2", "ask-A")
    b = sf.family_scratch_dir("C2", "ask-B")
    assert a != b and a.endswith("/C2") and "ask-A" in a


def test_unknown_run_uses_token_under_scratch():
    d = sf.family_scratch_dir("C2")
    assert d == f"scratch/family-stage/{sf.RUN_ID_TOKEN}/C2"
    sf.runner_brief("agy", "other", "p.md", d)


def test_brief_states_fresh_invoke_or_blocked():
    b = sf.runner_brief("agy", "write", "p.md", sf.family_scratch_dir("C2", "r1"), ["a.py"])
    assert sf.FRESH_INVOKE_LINE in b and "BLOCKED" in b
    assert "scratch: scratch/family-stage/r1/C2" in b


def _stage_family_rows(tmp_path, plan_task_extra, **sizing_over):
    import yaml

    from coordinator_core.ops.dispatch_emit.ask_stage import stage
    from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    write_run_scripts(tmp_path, "r1", "x1run")
    plan = (
        "---\ndeliverable_id: dlv-family-abc123\n---\n# P\n\n## Problem\n\nx\n\n## Tasks\n\n"
        "```yaml plan-tasks\n- id: C2\n  title: t\n  change_kind: code-edit\n  surface: pkg/b.py\n"
        f"  writes:\n    - pkg/b.py\n{plan_task_extra}```\n"
    )
    (tmp_path / "docs/plans/p.md").write_text(plan, encoding="utf-8", newline="\n")
    sizing = {"intent": "Fix", "exit_criterion": {"statement": "ok"}, "deliverable_id": "dlv-xs-abc123"}
    sizing.update(sizing_over)
    (tmp_path / "state/sizings/s.yaml").write_text(yaml.safe_dump(sizing), encoding="utf-8", newline="\n")
    m = stage(tmp_path, run_id="r1", plan_rel="docs/plans/p.md", commit_sizing_rel="state/sizings/s.yaml")
    return m.rows[0]


def test_staged_family_brief_carries_this_runs_dir_never_the_token(tmp_path):
    row = _stage_family_rows(tmp_path / "a", "", family="agy")
    for rel in (row.brief_path, row.fallback_brief_path):
        text = (tmp_path / "a" / rel).read_text(encoding="utf-8")
        assert sf.RUN_ID_TOKEN not in text
    brief = (tmp_path / "a" / row.brief_path).read_text(encoding="utf-8")
    assert "scratch/family-stage/r1/C2" in brief


def test_staged_per_row_family_brief_carries_this_runs_dir_never_the_token(tmp_path):
    row = _stage_family_rows(tmp_path / "b", "  family: agy\n")
    brief = (tmp_path / "b" / row.brief_path).read_text(encoding="utf-8")
    assert sf.RUN_ID_TOKEN not in brief
    assert "scratch/family-stage/r1/C2" in brief


def test_staged_family_brief_carries_fresh_invoke_line_through_token_substitution(tmp_path):
    row = _stage_family_rows(tmp_path / "c", "", family="agy")
    brief = (tmp_path / "c" / row.brief_path).read_text(encoding="utf-8")
    assert sf.FRESH_INVOKE_LINE in brief
    assert sf.RUN_ID_TOKEN not in brief
    assert "scratch/family-stage/r1/C2" in brief
