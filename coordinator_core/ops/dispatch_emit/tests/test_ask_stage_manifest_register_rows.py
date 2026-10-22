"""ask_stage carries each spine row's register_rows onto its ManifestRow; an absent field stays ()."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_stage import stage

PLAN_REL = "docs/plans/2026-10-01-regrows.md"


def _row(rid: str, extra: str = "") -> str:
    return (
        f"- id: {rid}\n  title: row {rid}\n  change_kind: code-edit\n  surface: pkg/{rid}.py\n"
        f"  writes:\n    - pkg/{rid}.py\n{extra}"
    )


def _stage(tmp_path, rows):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    text = "---\ndeliverable_id: dlv-reg-abc123\n---\n# Reg plan\n\n## Tasks\n\n```yaml plan-tasks\n" + "".join(rows) + "```\n"
    (tmp_path / PLAN_REL).write_text(text, encoding="utf-8", newline="\n")
    return stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)


def test_only_the_declaring_row_carries_register_rows(tmp_path):
    m = _stage(tmp_path, [_row("C1", "  register_rows: [R1, R2]\n"), _row("C2")])
    by_id = {r.id: r for r in m.rows}
    assert by_id["C1"].register_rows == ("R1", "R2")
    assert by_id["C2"].register_rows == ()
    assert "register_rows" not in by_id["C2"].to_json()
    assert by_id["C1"].to_json()["register_rows"] == ["R1", "R2"]
