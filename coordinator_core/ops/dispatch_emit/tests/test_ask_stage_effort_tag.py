"""ask_stage carries a row's effort tag into the manifest and never groups rows of differing effort."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_stage import stage

PLAN_REL = "docs/plans/2026-10-01-effort.md"


def _row(rid: str, effort: str | None) -> str:
    extra = f"  effort: {effort}\n" if effort else ""
    return (
        f"- id: {rid}\n  title: row {rid}\n  change_kind: code-edit\n  surface: pkg/{rid}.py\n"
        f"  writes:\n    - pkg/{rid}.py\n{extra}"
    )


def _stage(tmp_path, efforts):
    rows = "".join(_row(f"C{i}", e) for i, e in enumerate(efforts, 1))
    plan = (
        "---\ndeliverable_id: dlv-eff-abc123\n---\n# Effort plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        + rows + "```\n"
    )
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    return stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)


def test_effort_serialised_only_when_set_and_splits_groups(tmp_path):
    m = _stage(tmp_path, ["high", "high", None])
    by_id = {r.id: r for r in m.rows}
    js = {r.id: r.to_json() for r in m.rows}
    assert js["C1"]["effort"] == "high" and js["C2"]["effort"] == "high"
    assert "effort" not in js["C3"]
    assert by_id["C1"].group == by_id["C2"].group != ""
    assert by_id["C3"].group == ""
